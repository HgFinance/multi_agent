"""Shared, dependency-free contract for the executable alpha AST search surface.

Research intake and quant execution run in different container images.  This module is
copied into both and owns the grammar boundary so a scout cannot approve an expression
that the execution worker does not understand.
"""

from __future__ import annotations

import math

PRICE_FIELDS = ("close", "notional", "returns")
MICRO_FIELDS = (
    "spread_bps", "depth_imbalance", "order_flow_imbalance", "trade_intensity",
    "realized_volatility", "traded_value", "traded_volume", "ofi_close", "ofi_open",
    "ofi_intraday_std", "close_vs_vwap", "spread_close_ratio",
)
FIELDS = PRICE_FIELDS + MICRO_FIELDS

SOURCE_OPS = (
    "ts_last", "ts_mean", "ts_std", "ts_sum", "ts_delta", "ts_return", "ts_max",
    "ts_min", "ts_rank", "ts_corr",
)
UNARY_OPS = ("neg", "abs", "log", "sign", "sqrt", "rank", "zscore")
BINARY_OPS = ("add", "sub", "mul", "div")
ALL_OPS = SOURCE_OPS + UNARY_OPS + BINARY_OPS

MIN_WINDOW, MAX_WINDOW = 1, 250
MAX_DEPTH, MAX_NODES = 6, 40


def _window(node: dict, minimum: int) -> int:
    raw = node.get("n", 1)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"window n is not an integer: {raw!r}") from None
    if not (max(MIN_WINDOW, minimum) <= n <= MAX_WINDOW):
        raise ValueError(f"window n={n} is outside [{max(MIN_WINDOW, minimum)}, {MAX_WINDOW}]")
    return n


def _validate(node, depth: int = 1) -> dict:
    if depth > MAX_DEPTH:
        raise ValueError(f"expression depth exceeds {MAX_DEPTH}")
    if not isinstance(node, dict):
        raise ValueError("each AST node must be an object")
    if "const" in node:
        value = node["const"]
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(float(value))):
            raise ValueError(f"constant is not finite numeric: {value!r}")
        return {"const": float(value)}

    op = node.get("op")
    if op not in ALL_OPS:
        raise ValueError(f"unknown operator {op!r}")
    if op in SOURCE_OPS:
        if op == "ts_corr":
            fa, fb = node.get("field_a"), node.get("field_b")
            if fa not in FIELDS or fb not in FIELDS:
                raise ValueError(f"unknown correlation field: {fa!r}, {fb!r}")
            if fa == fb:
                raise ValueError("correlation fields must differ")
            return {"op": op, "field_a": fa, "field_b": fb,
                    "n": _window(node, 3)}
        field = node.get("field")
        if field not in FIELDS:
            raise ValueError(f"unknown field {field!r}")
        minimum = 2 if op in ("ts_std", "ts_delta", "ts_return", "ts_rank") else 1
        return {"op": op, "field": field, "n": _window(node, minimum)}
    if op in UNARY_OPS:
        if node.get("arg") is None:
            raise ValueError(f"{op} requires arg")
        return {"op": op, "arg": _validate(node["arg"], depth + 1)}
    args = node.get("args")
    if not isinstance(args, (list, tuple)) or len(args) != 2:
        raise ValueError(f"{op} requires two args")
    return {"op": op, "args": [_validate(arg, depth + 1) for arg in args]}


def count_nodes(node: dict) -> int:
    if "const" in node or node.get("op") in SOURCE_OPS:
        return 1
    if "arg" in node:
        return 1 + count_nodes(node["arg"])
    return 1 + sum(count_nodes(arg) for arg in node.get("args", ()))


def parse(node) -> dict:
    expr = _validate(node)
    nodes = count_nodes(expr)
    if nodes > MAX_NODES:
        raise ValueError(f"expression has {nodes} nodes; maximum is {MAX_NODES}")
    return expr


def fields_of(expr: dict) -> set[str]:
    if "const" in expr:
        return set()
    if expr.get("op") == "ts_corr":
        return {expr["field_a"], expr["field_b"]}
    if expr.get("op") in SOURCE_OPS:
        return {expr["field"]}
    if "arg" in expr:
        return fields_of(expr["arg"])
    fields: set[str] = set()
    for arg in expr.get("args", ()):
        fields |= fields_of(arg)
    return fields


def check_alignment(expr: dict, rationale: str) -> dict:
    """Require every executable field to be named in the scout's test specification."""
    fields = sorted(fields_of(expr))
    text = str(rationale or "").lower()
    unmentioned = [field for field in fields if field.lower() not in text]
    return {"fields": fields, "unmentioned": unmentioned, "ok": not unmentioned,
            "note": ("all AST fields are explicitly justified" if not unmentioned else
                     f"AST fields absent from mechanism/TESTABLE_WITH: {unmentioned}")}
