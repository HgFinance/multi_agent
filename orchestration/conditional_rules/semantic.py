"""Deterministic semantic and unit validation for conditional rules."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .contracts import (
    ActionSide,
    ConditionalRuleSpec,
    EvaluationClock,
    ExpressionNode,
    ExpressionType,
    IndicatorSource,
    SizingType,
    ValueUnit,
)


from .indicators import DEFAULT_REGISTRY, IndicatorDefinition


# Backward-compatible view for callers that imported the old constant.
# The registry remains authoritative.
INDICATORS: dict[str, IndicatorDefinition] = DEFAULT_REGISTRY.definitions

MARKET_FIELDS: dict[str, ValueUnit] = {
    "LAST_PRICE": ValueUnit.PRICE,
    "OPEN": ValueUnit.PRICE,
    "HIGH": ValueUnit.PRICE,
    "LOW": ValueUnit.PRICE,
    "CLOSE": ValueUnit.PRICE,
    "VOLUME": ValueUnit.VOLUME,
    # KOSPI values are read from the existing LS t1511 market-breadth stream,
    # never inferred from a constituent or an untrusted natural-language value.
    "KOSPI_DAILY_CLOSE": ValueUnit.PRICE,
    "KOSPI_DAILY_SMA_60": ValueUnit.PRICE,
    "KOSPI_DAY_CHANGE_RATIO": ValueUnit.RATIO,
}

PORTFOLIO_FIELDS: dict[str, ValueUnit] = {
    "POSITION_QUANTITY": ValueUnit.SHARES,
    "SELLABLE_QUANTITY": ValueUnit.SHARES,
    "AVG_ENTRY_PRICE": ValueUnit.PRICE,
    "MARKET_VALUE": ValueUnit.KRW,
    "PORTFOLIO_NAV": ValueUnit.KRW,
    "POSITION_WEIGHT": ValueUnit.RATIO,
    "UNREALIZED_PNL": ValueUnit.KRW,
    "PNL_PERCENT": ValueUnit.RATIO,
    "AVAILABLE_CASH": ValueUnit.KRW,
}

TIME_FIELDS: dict[str, ValueUnit] = {
    "OBSERVED_AT_EPOCH_SECONDS": ValueUnit.NUMBER,
    # KST wall-clock time of the authoritative quote or completed primary bar.
    # It is intentionally a scalar rather than a timezone supplied by Hermes:
    # domestic PAPER rules have one market clock and the worker/guard already
    # use the KRX session calendar.
    "KST_SECONDS_SINCE_MIDNIGHT": ValueUnit.NUMBER,
}


# Lower rank means a faster completed-bar cadence.  A BAR_CLOSE rule may use
# slower confirmation frames (for example 3M entry + 15M trend filter), but
# its primary clock may never be *slower* than a referenced frame or a stated
# 3-minute condition would quietly be checked only every five minutes.
_TIMEFRAME_RANK = {
    "1M": 1,
    "3M": 3,
    "5M": 5,
    "10M": 10,
    "15M": 15,
    "30M": 30,
    "1H": 60,
    "1D": 24 * 60,
}

# The runtime deliberately aggregates intraday frames from final 1M t8452
# rows, which has a 12 * 500 row guard.  The worker asks for two extra bars
# and the resolver asks for one aggregation bucket of margin, so validate the
# warm-up request here instead of creating an ACTIVE rule that can never load
# enough history.  1D uses t8451 and is bounded separately by the worker's
# 2,000-bar request ceiling.
_INTRADAY_FRAME_MINUTES = {
    "1M": 1,
    "3M": 3,
    "5M": 5,
    "10M": 10,
    "15M": 15,
    "30M": 30,
    "1H": 60,
}
_MAX_WORKER_HISTORY = 2000
_MAX_MINUTE_SOURCE_ROWS = 6000


class RuleSemanticError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _error(code: str, message: str) -> RuleSemanticError:
    return RuleSemanticError(code, message)


@dataclass(frozen=True)
class TrailingStopParameters:
    """Validated durable high-water exit parameters."""

    drawdown: Decimal
    drawdown_mode: str
    activation_return: Decimal | None
    expected_position_quantity: Decimal | None


@dataclass(frozen=True)
class TemporalSequenceParameters:
    """Bounded completed-bar memory for arm -> trigger/cancel rules."""

    window_bars: int


def temporal_sequence_parameters(node: ExpressionNode) -> TemporalSequenceParameters:
    if node.type is not ExpressionType.TEMPORAL_SEQUENCE:
        raise _error(
            "TEMPORAL_SEQUENCE_NODE_REQUIRED", "temporal sequence node required"
        )
    supplied = {
        str(key).upper(): value for key, value in (node.parameters or {}).items()
    }
    if set(supplied) != {"WINDOW_BARS"}:
        raise _error(
            "TEMPORAL_SEQUENCE_PARAMETER_INVALID",
            "temporal sequence requires only WINDOW_BARS",
        )
    try:
        value = Decimal(str(supplied["WINDOW_BARS"]))
    except Exception as exc:
        raise _error(
            "TEMPORAL_SEQUENCE_PARAMETER_INVALID",
            "WINDOW_BARS must be an integer in [1, 500]",
        ) from exc
    if (
        not value.is_finite()
        or value != value.to_integral_value()
        or not Decimal("1") <= value <= Decimal("500")
    ):
        raise _error(
            "TEMPORAL_SEQUENCE_PARAMETER_INVALID",
            "WINDOW_BARS must be an integer in [1, 500]",
        )
    return TemporalSequenceParameters(window_bars=int(value))


def trailing_stop_parameters(node: ExpressionNode) -> TrailingStopParameters:
    """Return the bounded high-water rule parameters from a trailing leaf.

    Ratios are decimal fractions: ``0.03`` means 3%.  They stay in the
    confirmed AST, while the mutable highest observed quote belongs only to
    the worker's database state.
    """

    if node.type is not ExpressionType.TRAILING_STOP:
        raise _error("TRAILING_STOP_NODE_REQUIRED", "trailing stop node required")
    supplied = {str(key).upper(): value for key, value in (node.parameters or {}).items()}
    allowed = {
        "DRAWDOWN",
        "DRAWDOWN_MODE",
        "ACTIVATION_RETURN",
        "EXPECTED_POSITION_QUANTITY",
    }
    unknown = set(supplied) - allowed
    if unknown:
        raise _error(
            "TRAILING_STOP_PARAMETER_UNSUPPORTED",
            f"trailing stop has unsupported parameters {sorted(unknown)}",
        )
    if "DRAWDOWN" not in supplied:
        raise _error(
            "MISSING_TRAILING_STOP_PARAMETER",
            "trailing stop requires DRAWDOWN",
        )

    def ratio(name: str, *, minimum: Decimal, maximum: Decimal) -> Decimal | None:
        if name not in supplied:
            return None
        try:
            value = Decimal(str(supplied[name]))
        except Exception as exc:
            raise _error(
                "INVALID_TRAILING_STOP_PARAMETER",
                f"{name} must be a finite ratio",
            ) from exc
        if not value.is_finite() or value < minimum or value >= maximum:
            raise _error(
                "INVALID_TRAILING_STOP_PARAMETER",
                f"{name} must be in [{minimum}, {maximum})",
            )
        return value

    drawdown = ratio("DRAWDOWN", minimum=Decimal("0.0001"), maximum=Decimal("1"))
    assert drawdown is not None
    drawdown_mode = str(supplied.get("DRAWDOWN_MODE", "PRICE_RATIO")).upper()
    if drawdown_mode not in {"PRICE_RATIO", "RETURN_POINTS"}:
        raise _error(
            "INVALID_TRAILING_STOP_PARAMETER",
            "DRAWDOWN_MODE must be PRICE_RATIO or RETURN_POINTS",
        )
    activation_return = ratio(
        "ACTIVATION_RETURN", minimum=Decimal("0"), maximum=Decimal("10")
    )
    expected_position_quantity: Decimal | None = None
    if "EXPECTED_POSITION_QUANTITY" in supplied:
        try:
            expected_position_quantity = Decimal(
                str(supplied["EXPECTED_POSITION_QUANTITY"])
            )
        except Exception as exc:
            raise _error(
                "INVALID_TRAILING_STOP_PARAMETER",
                "EXPECTED_POSITION_QUANTITY must be a positive whole-share quantity",
            ) from exc
        if (
            not expected_position_quantity.is_finite()
            or expected_position_quantity <= 0
            or expected_position_quantity
            != expected_position_quantity.to_integral_value()
        ):
            raise _error(
                "INVALID_TRAILING_STOP_PARAMETER",
                "EXPECTED_POSITION_QUANTITY must be a positive whole-share quantity",
            )
    return TrailingStopParameters(
        drawdown=drawdown,
        drawdown_mode=drawdown_mode,
        activation_return=activation_return,
        expected_position_quantity=expected_position_quantity,
    )


def _indicator_parameters(node: ExpressionNode) -> dict[str, Decimal | int | str]:
    definition = DEFAULT_REGISTRY.get(node.name)
    if definition is None:
        raise _error("UNSUPPORTED_INDICATOR", f"unsupported indicator {node.name!r}")
    supplied = {str(key).upper(): value for key, value in (node.parameters or {}).items()}
    unknown = set(supplied) - set(definition.defaults) - set(definition.required_parameters)
    missing = set(definition.required_parameters) - set(supplied)
    if unknown:
        raise _error(
            "UNSUPPORTED_INDICATOR_PARAMETER",
            f"{node.name} has unsupported parameters {sorted(unknown)}",
        )
    if missing:
        raise _error(
            "MISSING_INDICATOR_PARAMETER",
            f"{node.name} requires parameters {sorted(missing)}",
        )
    result: dict[str, Decimal | int | str] = dict(definition.defaults)
    for key, raw in supplied.items():
        if key in definition.string_parameters:
            if not isinstance(raw, str) or not raw.strip():
                raise _error("INVALID_INDICATOR_PARAMETER", f"{key} must be a non-empty string")
            result[key] = raw.strip()
            continue
        try:
            parsed = Decimal(str(raw))
        except Exception as exc:
            raise _error("INVALID_INDICATOR_PARAMETER", f"{key} is not numeric") from exc
        if not parsed.is_finite():
            raise _error("INVALID_INDICATOR_PARAMETER", f"{key} must be finite")
        if key in definition.zero_allowed_parameters:
            if parsed < 0:
                raise _error("INVALID_INDICATOR_PARAMETER", f"{key} must not be negative")
        elif parsed <= 0:
            raise _error("INVALID_INDICATOR_PARAMETER", f"{key} must be positive")
        if parsed > 500:
            raise _error(
                "INDICATOR_PARAMETER_TOO_LARGE",
                f"{key} exceeds the v1 completed-bar limit of 500",
            )
        if key in definition.integer_parameters:
            if parsed != parsed.to_integral_value():
                raise _error("INVALID_INDICATOR_PARAMETER", f"{key} must be an integer")
            result[key] = int(parsed)
        else:
            result[key] = parsed
    if DEFAULT_REGISTRY.canonical_name(node.name) == "MACD" and int(result["FAST"]) >= int(result["SLOW"]):
        raise _error("INVALID_INDICATOR_PARAMETER", "MACD FAST must be below SLOW")
    return result


def indicator_definition(node: ExpressionNode) -> IndicatorDefinition:
    definition = DEFAULT_REGISTRY.get(node.name)
    if definition is None:
        raise _error("UNSUPPORTED_INDICATOR", f"unsupported indicator {node.name!r}")
    requested_source = node.source.value if isinstance(node.source, IndicatorSource) else node.source
    if requested_source is not None and requested_source != definition.source:
        raise _error(
            "INDICATOR_SOURCE_MISMATCH",
            f"{definition.name} belongs to source {definition.source}, not {requested_source}",
        )
    if node.provider is not None:
        expected_provider = (definition.provider or definition.source).upper()
        if node.provider != expected_provider:
            raise _error(
                "INDICATOR_PROVIDER_MISMATCH",
                f"{definition.name} belongs to provider {expected_provider}, not {node.provider}",
            )
    return definition


def indicator_source(node: ExpressionNode) -> str:
    return indicator_definition(node).source


def _contains_type(node: ExpressionNode | None, expression_type: ExpressionType) -> bool:
    if node is None:
        return False
    if node.type is expression_type:
        return True
    return any(
        _contains_type(child, expression_type)
        for child in (node.left, node.right, node.operand, *(node.children or ()))
    )


def _walk_nodes(node: ExpressionNode | None):
    if node is None:
        return
    yield node
    for child in (node.left, node.right, node.operand, *(node.children or ())):
        yield from _walk_nodes(child)


def _bar_timeframes(node: ExpressionNode | None, *, primary: str) -> set[str]:
    """Return every completed-bar cadence consumed by an expression.

    MARKET leaves at BAR_CLOSE are the primary candle.  An indicator carries
    its own cadence.  Literal/portfolio/time leaves carry no candle cadence.
    This deliberately operates only on the typed AST; it never infers a
    timeframe from natural-language wording.
    """

    if node is None:
        return set()
    if node.type is ExpressionType.INDICATOR and node.timeframe is not None:
        return {node.timeframe.value}
    if node.type is ExpressionType.MARKET:
        return {primary}
    result: set[str] = set()
    for child in (node.left, node.right, node.operand, *(node.children or ())):
        result.update(_bar_timeframes(child, primary=primary))
    return result


def _validate_bar_timeframes(rule: ConditionalRuleSpec) -> None:
    """Make multi-timeframe rules explicit and temporally meaningful."""

    if rule.evaluation.clock is not EvaluationClock.BAR_CLOSE:
        return
    primary = rule.evaluation.primary_timeframe
    assert primary is not None  # EvaluationPolicy already enforces this.
    primary_name = primary.value
    used = _bar_timeframes(rule.condition, primary=primary_name)
    if any(_TIMEFRAME_RANK[primary_name] > _TIMEFRAME_RANK[item] for item in used):
        raise _error(
            "PRIMARY_TIMEFRAME_TOO_SLOW",
            "primary_timeframe must be at least as fast as every referenced bar timeframe",
        )

    def visit(node: ExpressionNode | None) -> None:
        if node is None:
            return
        if node.type is ExpressionType.CROSS:
            left = _bar_timeframes(node.left, primary=primary_name)
            right = _bar_timeframes(node.right, primary=primary_name)
            if len(left | right) > 1:
                raise _error(
                    "CROSS_TIMEFRAME_MISMATCH",
                    "CROSS operands must use one completed-bar timeframe; use LOGICAL AND for multi-timeframe confirmation",
                )
        for child in (node.left, node.right, node.operand, *(node.children or ())):
            visit(child)

    visit(rule.condition)


def _validate_runtime_history(rule: ConditionalRuleSpec) -> None:
    """Reject local-indicator warm-ups the PAPER resolver cannot supply."""

    needs_previous = _contains_type(rule.condition, ExpressionType.CROSS)
    for node in _walk_nodes(rule.condition):
        if node.type is not ExpressionType.INDICATOR or node.timeframe is None:
            continue
        definition = DEFAULT_REGISTRY.get(node.name)
        if definition is None or definition.source != "LOCAL":
            continue
        required = definition.required_history(normalized_indicator_parameters(node))
        if needs_previous:
            required += 1
        timeframe = node.timeframe.value
        if timeframe == "1D":
            maximum = _MAX_WORKER_HISTORY - 2
        else:
            step = _INTRADAY_FRAME_MINUTES[timeframe]
            # resolver needs ``(required + 2) * step + step`` final 1M rows.
            maximum = min(
                _MAX_WORKER_HISTORY - 2,
                _MAX_MINUTE_SOURCE_ROWS // step - 3,
            )
        if required > maximum:
            raise _error(
                "INDICATOR_HISTORY_UNAVAILABLE",
                f"{definition.name} on {timeframe} requires {required} completed bars; PAPER resolver limit is {maximum}",
            )


def _validate_time_windows(rule: ConditionalRuleSpec) -> None:
    """Keep KST wall-clock time as a precise, bounded window predicate.

    A generic epoch timestamp can support the existing one-off delayed-order
    trigger.  A repeating intraday window is different: it must be an explicit
    comparison of KST seconds to a bounded numeric literal.  Rejecting CROSS,
    arithmetic, and unbounded/malformed values prevents a natural-language
    interpreter from turning "10:00~14:30" into a hidden timestamp formula.
    """

    valid: set[int] = set()
    for node in _walk_nodes(rule.condition):
        if node.type is not ExpressionType.COMPARISON:
            continue
        time_node, literal = node.left, node.right
        if (
            time_node is None
            or time_node.type is not ExpressionType.TIME
            or time_node.field != "KST_SECONDS_SINCE_MIDNIGHT"
        ):
            continue
        if node.operator not in {"GT", "GTE", "LT", "LTE"}:
            raise _error(
                "TIME_WINDOW_OPERATOR_UNSUPPORTED",
                "KST time windows support only GT/GTE/LT/LTE comparisons",
            )
        if (
            literal is None
            or literal.type is not ExpressionType.LITERAL
            or literal.unit is not ValueUnit.NUMBER
            or isinstance(literal.value, bool)
        ):
            raise _error(
                "TIME_WINDOW_LITERAL_INVALID",
                "KST time window requires a numeric seconds literal",
            )
        try:
            seconds = Decimal(str(literal.value))
        except Exception as exc:  # pragma: no cover - Pydantic already normalizes Decimal
            raise _error(
                "TIME_WINDOW_LITERAL_INVALID",
                "KST time window requires a numeric seconds literal",
            ) from exc
        if (
            not seconds.is_finite()
            or seconds != seconds.to_integral_value()
            or not Decimal("0") <= seconds < Decimal("86400")
        ):
            raise _error(
                "TIME_WINDOW_LITERAL_INVALID",
                "KST time window seconds must be an integer in [0, 86400)",
            )
        valid.add(id(time_node))

    for node in _walk_nodes(rule.condition):
        if (
            node.type is ExpressionType.TIME
            and node.field == "KST_SECONDS_SINCE_MIDNIGHT"
            and id(node) not in valid
        ):
            raise _error(
                "TIME_WINDOW_SHAPE_INVALID",
                "KST time must be directly compared with a numeric seconds literal",
            )


def normalized_indicator_parameters(node: ExpressionNode) -> dict[str, Decimal | int | str]:
    """Return defaults plus validated explicit parameters for evaluation."""

    return _indicator_parameters(node)


def _arithmetic_unit(operator: str, left: ValueUnit, right: ValueUnit) -> ValueUnit:
    if operator in {"ADD", "SUB"}:
        if left is not right or left is ValueUnit.BOOL:
            raise _error("UNIT_MISMATCH", f"{operator} requires identical numeric units")
        return left
    if operator == "MUL":
        if left in {ValueUnit.NUMBER, ValueUnit.RATIO} and right is not ValueUnit.BOOL:
            return right
        if right in {ValueUnit.NUMBER, ValueUnit.RATIO} and left is not ValueUnit.BOOL:
            return left
        raise _error("UNSUPPORTED_UNIT_ARITHMETIC", f"cannot multiply {left} by {right}")
    if operator == "DIV":
        if left is right and left is not ValueUnit.BOOL:
            return ValueUnit.RATIO
        if right in {ValueUnit.NUMBER, ValueUnit.RATIO} and left is not ValueUnit.BOOL:
            return left
        raise _error("UNSUPPORTED_UNIT_ARITHMETIC", f"cannot divide {left} by {right}")
    raise _error("UNSUPPORTED_ARITHMETIC_OPERATOR", f"unsupported operator {operator!r}")


def _infer(
    node: ExpressionNode,
    *,
    clock: EvaluationClock,
    depth: int,
    counter: list[int],
) -> ValueUnit:
    counter[0] += 1
    if counter[0] > 128 or depth > 16:
        raise _error("EXPRESSION_TOO_COMPLEX", "expression exceeds v1 complexity limits")

    if node.type is ExpressionType.LITERAL:
        if isinstance(node.value, Decimal) and not node.value.is_finite():
            raise _error("NON_FINITE_LITERAL", "literal must be finite")
        if node.unit is ValueUnit.BOOL and not isinstance(node.value, bool):
            raise _error("LITERAL_TYPE_MISMATCH", "BOOL literal requires boolean value")
        if node.unit is not ValueUnit.BOOL and isinstance(node.value, bool):
            raise _error("LITERAL_TYPE_MISMATCH", "numeric literal must not be boolean")
        return node.unit or ValueUnit.NUMBER

    if node.type is ExpressionType.TIME:
        unit = TIME_FIELDS.get(node.field or "")
        if unit is None:
            raise _error("UNSUPPORTED_TIME_FIELD", f"unsupported time field {node.field!r}")
        return unit

    if node.type is ExpressionType.MARKET:
        unit = MARKET_FIELDS.get(node.field or "")
        if unit is None:
            raise _error("UNSUPPORTED_MARKET_FIELD", f"unsupported market field {node.field!r}")
        if clock is EvaluationClock.QUOTE and node.field not in {
            "LAST_PRICE",
            "KOSPI_DAILY_CLOSE",
            "KOSPI_DAILY_SMA_60",
            "KOSPI_DAY_CHANGE_RATIO",
        }:
            raise _error("QUOTE_FIELD_UNAVAILABLE", f"{node.field} requires completed bars")
        return unit

    if node.type is ExpressionType.PORTFOLIO:
        unit = PORTFOLIO_FIELDS.get(node.field or "")
        if unit is None:
            raise _error(
                "UNSUPPORTED_PORTFOLIO_FIELD",
                f"unsupported portfolio field {node.field!r}",
            )
        return unit

    if node.type is ExpressionType.INDICATOR:
        definition = indicator_definition(node)
        if clock is EvaluationClock.QUOTE and not definition.realtime_supported:
            raise _error("INDICATOR_REQUIRES_BAR_CLOSE", "indicator requires completed bars and is not available on quote clock")
        if clock is EvaluationClock.BAR_CLOSE and not definition.historical_supported:
            raise _error("INDICATOR_REQUIRES_REALTIME", "indicator is not available on completed bars")
        timeframe = node.timeframe.value if node.timeframe is not None else None
        if timeframe not in definition.supported_timeframes:
            raise _error(
                "UNSUPPORTED_INDICATOR_TIMEFRAME",
                f"{definition.name} does not support timeframe {timeframe}",
            )
        _indicator_parameters(node)
        output = (node.output or "VALUE").upper()
        if output not in definition.outputs:
            raise _error(
                "UNSUPPORTED_INDICATOR_OUTPUT",
                f"{node.name} does not expose {output}",
            )
        return definition.outputs[output]

    if node.type is ExpressionType.TRAILING_STOP:
        trailing_stop_parameters(node)
        return ValueUnit.BOOL

    if node.type is ExpressionType.TEMPORAL_SEQUENCE:
        temporal_sequence_parameters(node)
        if clock is not EvaluationClock.BAR_CLOSE:
            raise _error(
                "TEMPORAL_SEQUENCE_REQUIRES_BAR_CLOSE",
                "temporal sequence requires completed bars",
            )
        for child in node.children or ():
            if _infer(child, clock=clock, depth=depth + 1, counter=counter) is not ValueUnit.BOOL:
                raise _error(
                    "TEMPORAL_SEQUENCE_REQUIRES_BOOL",
                    "temporal sequence children must be boolean",
                )
        return ValueUnit.BOOL

    if node.type is ExpressionType.ARITHMETIC:
        left = _infer(node.left, clock=clock, depth=depth + 1, counter=counter)  # type: ignore[arg-type]
        right = _infer(node.right, clock=clock, depth=depth + 1, counter=counter)  # type: ignore[arg-type]
        if (
            node.operator == "DIV"
            and node.right is not None
            and node.right.type is ExpressionType.LITERAL
            and Decimal(str(node.right.value)) == 0
        ):
            raise _error("DIVISION_BY_ZERO", "literal divisor must not be zero")
        return _arithmetic_unit(node.operator or "", left, right)

    if node.type in {ExpressionType.COMPARISON, ExpressionType.CROSS}:
        left = _infer(node.left, clock=clock, depth=depth + 1, counter=counter)  # type: ignore[arg-type]
        right = _infer(node.right, clock=clock, depth=depth + 1, counter=counter)  # type: ignore[arg-type]
        boolean_atoms = {ExpressionType.LITERAL, ExpressionType.INDICATOR}
        if (
            node.type is ExpressionType.COMPARISON
            and left is right is ValueUnit.BOOL
            and node.left is not None
            and node.right is not None
            and node.left.type in boolean_atoms
            and node.right.type in boolean_atoms
        ):
            if node.operator != "EQ":
                raise _error("BOOLEAN_COMPARISON_UNSUPPORTED", "boolean comparison supports EQ only")
            return ValueUnit.BOOL
        if ValueUnit.BOOL in {left, right}:
            raise _error(
                "BOOLEAN_COMPARISON_UNSUPPORTED",
                "comparison and cross operands must be numeric unless both are BOOL EQ",
            )
        if left is not right and {left, right} != {ValueUnit.NUMBER, ValueUnit.RATIO}:
            raise _error("UNIT_MISMATCH", f"cannot compare {left} with {right}")
        allowed = (
            {"GT", "GTE", "LT", "LTE", "EQ"}
            if node.type is ExpressionType.COMPARISON
            else {"ABOVE", "BELOW"}
        )
        if node.operator not in allowed:
            raise _error("UNSUPPORTED_BOOLEAN_OPERATOR", f"unsupported operator {node.operator!r}")
        if node.type is ExpressionType.CROSS and clock is not EvaluationClock.BAR_CLOSE:
            raise _error("CROSS_REQUIRES_BAR_CLOSE", "cross requires two completed observations")
        if node.type is ExpressionType.CROSS and (
            _contains_type(node.left, ExpressionType.PORTFOLIO)
            or _contains_type(node.right, ExpressionType.PORTFOLIO)
        ):
            raise _error(
                "CROSS_PORTFOLIO_UNSUPPORTED",
                "portfolio values do not have a durable previous-bar snapshot in v1",
            )
        return ValueUnit.BOOL

    if node.type is ExpressionType.LOGICAL:
        if node.operator not in {"AND", "OR"}:
            raise _error("UNSUPPORTED_LOGICAL_OPERATOR", f"unsupported operator {node.operator!r}")
        for child in node.children or ():
            unit = _infer(child, clock=clock, depth=depth + 1, counter=counter)
            if unit is not ValueUnit.BOOL:
                raise _error("LOGICAL_REQUIRES_BOOL", "logical operands must be boolean")
        return ValueUnit.BOOL

    if node.type is ExpressionType.NOT:
        unit = _infer(node.operand, clock=clock, depth=depth + 1, counter=counter)  # type: ignore[arg-type]
        if unit is not ValueUnit.BOOL:
            raise _error("LOGICAL_REQUIRES_BOOL", "NOT operand must be boolean")
        return ValueUnit.BOOL

    raise _error("UNSUPPORTED_EXPRESSION", f"unsupported expression type {node.type}")


def validate_rule_spec(rule: ConditionalRuleSpec) -> ConditionalRuleSpec:
    """Return the rule after complete deterministic semantic validation."""

    result = _infer(rule.condition, clock=rule.evaluation.clock, depth=1, counter=[0])
    if result is not ValueUnit.BOOL:
        raise _error("CONDITION_NOT_BOOLEAN", "rule condition must evaluate to BOOL")
    _validate_bar_timeframes(rule)
    _validate_runtime_history(rule)
    _validate_time_windows(rule)
    trailing_nodes = [
        node for node in _walk_nodes(rule.condition)
        if node.type is ExpressionType.TRAILING_STOP
    ]
    if trailing_nodes:
        if len(trailing_nodes) != 1 or rule.condition.type is not ExpressionType.TRAILING_STOP:
            raise _error(
                "TRAILING_STOP_COMPOSITION_UNSUPPORTED",
                "trailing stop must be the complete condition in this version",
            )
        if rule.evaluation.clock is not EvaluationClock.QUOTE:
            raise _error(
                "TRAILING_STOP_REQUIRES_QUOTE",
                "trailing stop requires the fresh quote clock",
            )
        if rule.action.side is not ActionSide.SELL:
            raise _error(
                "TRAILING_STOP_SELL_ONLY",
                "trailing stop is an existing-position SELL exit only",
            )
    temporal_nodes = [
        node
        for node in _walk_nodes(rule.condition)
        if node.type is ExpressionType.TEMPORAL_SEQUENCE
    ]
    if temporal_nodes and (
        len(temporal_nodes) != 1
        or rule.condition.type is not ExpressionType.TEMPORAL_SEQUENCE
    ):
        raise _error(
            "TEMPORAL_SEQUENCE_COMPOSITION_UNSUPPORTED",
            "temporal sequence must be the complete condition",
        )
    if (
        rule.action.side is ActionSide.SELL
        and rule.action.sizing.type is SizingType.FIXED_SHARES
    ):
        # Fixed sells are supported; the execution guard still clamps nothing
        # and rejects if current sellable quantity is insufficient.
        return rule
    return rule


__all__ = [
    "INDICATORS",
    "indicator_definition",
    "indicator_source",
    "MARKET_FIELDS",
    "PORTFOLIO_FIELDS",
    "IndicatorDefinition",
    "RuleSemanticError",
    "TrailingStopParameters",
    "TemporalSequenceParameters",
    "normalized_indicator_parameters",
    "trailing_stop_parameters",
    "temporal_sequence_parameters",
    "validate_rule_spec",
]
