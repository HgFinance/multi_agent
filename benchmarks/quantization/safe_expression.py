"""Safe, generic expression extraction and evaluation for LLM tool use.

The model is responsible for translating a user's calculation into an
expression.  This module only executes a deliberately small arithmetic
language; it contains no benchmark IDs, answer keys, or finance-specific
rules.
"""

from __future__ import annotations

import ast
import math
import operator
import re
from dataclasses import dataclass
from typing import Any


class ExpressionError(ValueError):
    """Raised when an LLM expression is missing, unsafe, or invalid."""


_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
}
_UNARY_OPERATORS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_EXPR_CHARS = re.compile(r"^[0-9eE+*/().,\s_-]+$")
_EXPR_PREFIX = re.compile(r"(?im)^\s*EXPR\s*:\s*(?P<expr>[^\n\r]+)")
_MAX_NODES = 64
_MAX_ABS_VALUE = 10**30
_MAX_EXPONENT = 100


@dataclass(frozen=True)
class ExpressionResult:
    expression: str
    value: int | float


def extract_expression(raw: str) -> str:
    """Extract an explicit ``EXPR:`` expression, never arbitrary prose."""

    text = str(raw).strip()
    match = _EXPR_PREFIX.search(text)
    if not match:
        raise ExpressionError("missing EXPR: prefix")

    expression = match.group("expr").strip()
    if not expression or not _EXPR_CHARS.fullmatch(expression):
        raise ExpressionError("expression contains unsupported characters")
    # Thousands separators are presentation syntax, not an operator. Remove
    # only commas between digits; any other comma remains invalid.
    expression = re.sub(r"(?<=\d),(?=\d)", "", expression)
    if "," in expression:
        raise ExpressionError("comma is only allowed as a thousands separator")
    if "--" in expression or "//" in expression:
        raise ExpressionError("unsupported operator spelling")
    return expression


def _check_number(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExpressionError("only finite numeric constants are allowed")
    if not math.isfinite(float(value)) or abs(value) > _MAX_ABS_VALUE:
        raise ExpressionError("numeric value exceeds safe bounds")
    return value


def _evaluate(node: ast.AST, depth: int = 0) -> int | float:
    if depth > _MAX_NODES:
        raise ExpressionError("expression is too deep")

    if isinstance(node, ast.Constant):
        return _check_number(node.value)

    if isinstance(node, ast.UnaryOp):
        operation = _UNARY_OPERATORS.get(type(node.op))
        if operation is None:
            raise ExpressionError("unary operator is not allowed")
        return _check_number(operation(_evaluate(node.operand, depth + 1)))

    if isinstance(node, ast.BinOp):
        operation = _BINARY_OPERATORS.get(type(node.op))
        if operation is None:
            raise ExpressionError("binary operator is not allowed")
        left = _evaluate(node.left, depth + 1)
        right = _evaluate(node.right, depth + 1)
        if isinstance(node.op, ast.Pow) and abs(float(right)) > _MAX_EXPONENT:
            raise ExpressionError("exponent exceeds safe bounds")
        try:
            return _check_number(operation(left, right))
        except (ArithmeticError, OverflowError) as exc:
            raise ExpressionError(f"arithmetic failed: {exc}") from exc

    raise ExpressionError(f"syntax node is not allowed: {type(node).__name__}")


def evaluate_expression(expression: str) -> int | float:
    """Evaluate only numeric literals and safe arithmetic AST nodes."""

    expression = re.sub(r"(?<=\d),(?=\d)", "", expression.strip())
    if not _EXPR_CHARS.fullmatch(expression) or "," in expression:
        raise ExpressionError("expression contains unsupported characters")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"invalid expression syntax: {exc.msg}") from exc
    return _evaluate(tree.body)


def evaluate_response(raw: str) -> ExpressionResult:
    expression = extract_expression(raw)
    return ExpressionResult(expression, evaluate_expression(expression))


def format_value(value: int | float) -> str:
    """Produce a stable scalar response without adding domain semantics."""

    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return format(value, ".12g")
