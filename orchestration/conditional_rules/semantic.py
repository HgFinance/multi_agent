"""Deterministic semantic and unit validation for conditional rules."""

from __future__ import annotations

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
}


class RuleSemanticError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _error(code: str, message: str) -> RuleSemanticError:
    return RuleSemanticError(code, message)


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
        if clock is EvaluationClock.QUOTE and node.field != "LAST_PRICE":
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
    "normalized_indicator_parameters",
    "validate_rule_spec",
]
