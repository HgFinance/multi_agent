"""Causal, unit-checked AST contract for event-time microstructure hypotheses.

The daily AST uses trading-day windows over cross-sectional rows.  Reusing that
grammar for tick research silently changes ``n=5`` from days to observations and
cannot express multi-second persistence.  This grammar makes the clock explicit,
checks physical units, and evaluates every temporal node using only samples whose
decision timestamp is no later than the current decision.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
import hashlib
import json
import math
from statistics import fmean, pstdev


AST_VERSION = "intraday-alpha-ast-v1"
MAX_DEPTH = 8
MAX_NODES = 48
MIN_SECONDS = 1
MAX_SECONDS = 3600

RATIO = "RATIO"
BPS = "BPS"
SHARES = "SHARES"
COUNT = "COUNT"
MILLISECONDS = "MILLISECONDS"
PER_SECOND = "PER_SECOND"
BOOL = "BOOL"

FIELDS = {
    "queue_imbalance_l1": RATIO,
    "queue_imbalance_l10": RATIO,
    "microprice_offset_bps": BPS,
    "trade_flow_imbalance": RATIO,
    "quote_event_ofi": SHARES,
    "normalized_quote_ofi": RATIO,
    "spread_bps": BPS,
    "bid_depth_l1": SHARES,
    "ask_depth_l1": SHARES,
    "book_depth_l1": SHARES,
    "book_depth_l10": SHARES,
    "trade_count": COUNT,
    "quote_count": COUNT,
    "trade_intensity": PER_SECOND,
    "realized_volatility_bps": BPS,
    "quote_age_ms": MILLISECONDS,
}

FIELD_OP = "field"
TEMPORAL_OPS = frozenset({"lag", "rolling_mean", "rolling_std", "rolling_sum",
                          "delta", "ewma", "rolling_zscore"})
UNARY_OPS = frozenset({"neg", "abs", "sign", "log1p_abs", "sqrt_abs"})
BINARY_OPS = frozenset({"add", "sub", "mul", "div", "min", "max", "gt", "lt",
                        "and", "or"})
TERNARY_OPS = frozenset({"where"})
ALL_OPS = frozenset({FIELD_OP}) | TEMPORAL_OPS | UNARY_OPS | BINARY_OPS | TERNARY_OPS


class IntradayExprError(ValueError):
    pass


def _unknown(node: dict, allowed: set[str], kind: str) -> None:
    extra = sorted(set(node) - allowed)
    if extra:
        raise IntradayExprError(f"unknown {kind} keys: {extra}")


def _seconds(node: dict) -> int:
    raw = node.get("seconds")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise IntradayExprError(f"seconds must be an integer: {raw!r}") from None
    if not MIN_SECONDS <= value <= MAX_SECONDS:
        raise IntradayExprError(
            f"seconds={value} outside [{MIN_SECONDS}, {MAX_SECONDS}]")
    return value


def _parse(node, depth: int) -> dict:
    if depth > MAX_DEPTH:
        raise IntradayExprError(f"expression depth exceeds {MAX_DEPTH}")
    if not isinstance(node, dict):
        raise IntradayExprError("each AST node must be an object")
    if "const" in node:
        _unknown(node, {"const", "unit"}, "constant")
        value = node["const"]
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(float(value))):
            raise IntradayExprError(f"constant is not finite numeric: {value!r}")
        unit = str(node.get("unit") or RATIO).upper()
        if unit not in {RATIO, BPS, SHARES, COUNT, MILLISECONDS, PER_SECOND}:
            raise IntradayExprError(f"unknown constant unit {unit!r}")
        return {"const": float(value), "unit": unit}

    op = str(node.get("op") or "")
    if op not in ALL_OPS:
        raise IntradayExprError(f"unknown operator {op!r}")
    if op == FIELD_OP:
        _unknown(node, {"op", "field"}, op)
        field = str(node.get("field") or "")
        if field not in FIELDS:
            raise IntradayExprError(f"unknown intraday field {field!r}")
        return {"op": op, "field": field}
    if op in TEMPORAL_OPS:
        _unknown(node, {"op", "arg", "seconds"}, op)
        if node.get("arg") is None:
            raise IntradayExprError(f"{op} requires arg")
        return {"op": op, "arg": _parse(node["arg"], depth + 1),
                "seconds": _seconds(node)}
    if op in UNARY_OPS:
        _unknown(node, {"op", "arg"}, op)
        if node.get("arg") is None:
            raise IntradayExprError(f"{op} requires arg")
        return {"op": op, "arg": _parse(node["arg"], depth + 1)}
    if op in BINARY_OPS:
        _unknown(node, {"op", "args"}, op)
        args = node.get("args")
        if not isinstance(args, (list, tuple)) or len(args) != 2:
            raise IntradayExprError(f"{op} requires exactly two args")
        return {"op": op, "args": [_parse(arg, depth + 1) for arg in args]}
    _unknown(node, {"op", "condition", "then", "else"}, op)
    if any(node.get(key) is None for key in ("condition", "then", "else")):
        raise IntradayExprError("where requires condition, then, and else")
    return {"op": op,
            "condition": _parse(node["condition"], depth + 1),
            "then": _parse(node["then"], depth + 1),
            "else": _parse(node["else"], depth + 1)}


def count_nodes(node: dict) -> int:
    if "const" in node or node.get("op") == FIELD_OP:
        return 1
    if "arg" in node:
        return 1 + count_nodes(node["arg"])
    if "args" in node:
        return 1 + sum(count_nodes(arg) for arg in node["args"])
    return (1 + count_nodes(node["condition"]) + count_nodes(node["then"])
            + count_nodes(node["else"]))


def _unit(node: dict) -> str:
    if "const" in node:
        return node.get("unit", RATIO)
    op = node["op"]
    if op == FIELD_OP:
        return FIELDS[node["field"]]
    if op in TEMPORAL_OPS:
        source = _unit(node["arg"])
        if source == BOOL:
            raise IntradayExprError(f"{op} cannot aggregate BOOL")
        return RATIO if op == "rolling_zscore" else source
    if op in UNARY_OPS:
        source = _unit(node["arg"])
        if source == BOOL:
            raise IntradayExprError(f"{op} cannot transform BOOL")
        return RATIO if op in {"sign", "log1p_abs", "sqrt_abs"} else source
    if op in {"and", "or"}:
        units = [_unit(arg) for arg in node["args"]]
        if units != [BOOL, BOOL]:
            raise IntradayExprError(f"{op} requires BOOL args, got {units}")
        return BOOL
    if op in {"gt", "lt"}:
        left, right = (_unit(arg) for arg in node["args"])
        if left != right:
            raise IntradayExprError(f"{op} compares incompatible units {left}/{right}")
        return BOOL
    if op == "where":
        condition = _unit(node["condition"])
        left, right = _unit(node["then"]), _unit(node["else"])
        if condition != BOOL or left != right:
            raise IntradayExprError(
                f"where needs BOOL and equal branch units, got {condition}/{left}/{right}")
        return left

    left, right = (_unit(arg) for arg in node["args"])
    if op in {"add", "sub", "min", "max"}:
        if left != right:
            raise IntradayExprError(f"{op} combines incompatible units {left}/{right}")
        return left
    if op == "mul":
        if left == RATIO:
            return right
        if right == RATIO:
            return left
        raise IntradayExprError(f"mul requires a dimensionless side, got {left}/{right}")
    if op == "div":
        if right == RATIO:
            return left
        if left == right:
            return RATIO
        raise IntradayExprError(f"div has incompatible units {left}/{right}")
    raise IntradayExprError(f"unit rule missing for {op}")


def parse(node) -> dict:
    expr = _parse(node, 1)
    if count_nodes(expr) > MAX_NODES:
        raise IntradayExprError(
            f"expression has {count_nodes(expr)} nodes; maximum is {MAX_NODES}")
    output = _unit(expr)
    if output == BOOL:
        raise IntradayExprError("top-level signal must be numeric, not BOOL")
    return expr


def unit_of(expr: dict) -> str:
    return _unit(parse(expr))


def _canonical(node: dict, *, shape: bool = False) -> dict:
    if "const" in node:
        return {"const": "#" if shape else node["const"],
                "unit": node.get("unit", RATIO)}
    if node["op"] == FIELD_OP:
        return node
    if "arg" in node:
        out = {"op": node["op"], "arg": _canonical(node["arg"], shape=shape)}
        if "seconds" in node:
            out["seconds"] = "#" if shape else node["seconds"]
        return out
    if "args" in node:
        args = [_canonical(arg, shape=shape) for arg in node["args"]]
        if node["op"] in {"add", "mul", "min", "max", "and", "or"}:
            args.sort(key=lambda value: json.dumps(value, sort_keys=True))
        return {"op": node["op"], "args": args}
    return {"op": "where",
            "condition": _canonical(node["condition"], shape=shape),
            "then": _canonical(node["then"], shape=shape),
            "else": _canonical(node["else"], shape=shape)}


def fingerprint(expr: dict) -> str:
    payload = json.dumps(_canonical(parse(expr)), sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def shape_fingerprint(expr: dict) -> str:
    payload = json.dumps(_canonical(parse(expr), shape=True), sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def fields_of(expr: dict) -> set[str]:
    node = parse(expr)
    out: set[str] = set()

    def walk(current: dict) -> None:
        if current.get("op") == FIELD_OP:
            out.add(current["field"])
        if "arg" in current:
            walk(current["arg"])
        for child in current.get("args", ()):
            walk(child)
        for key in ("condition", "then", "else"):
            if key in current:
                walk(current[key])
    walk(node)
    return out


def clocks_of(expr: dict) -> set[int]:
    node = parse(expr)
    out: set[int] = set()

    def walk(current: dict) -> None:
        if "seconds" in current:
            out.add(int(current["seconds"]))
        if "arg" in current:
            walk(current["arg"])
        for child in current.get("args", ()):
            walk(child)
        for key in ("condition", "then", "else"):
            if key in current:
                walk(current[key])
    walk(node)
    return out


def operators_of(expr: dict) -> set[str]:
    node = parse(expr)
    out: set[str] = set()

    def walk(current: dict) -> None:
        if current.get("op"):
            out.add(current["op"])
        if "arg" in current:
            walk(current["arg"])
        for child in current.get("args", ()):
            walk(child)
        for key in ("condition", "then", "else"):
            if key in current:
                walk(current[key])
    walk(node)
    return out


def conditional_fields_of(expr: dict) -> set[str]:
    """Fields that actually gate a ``where`` branch, not merely decorate output."""
    node = parse(expr)
    out: set[str] = set()

    def collect_fields(current: dict) -> None:
        if current.get("op") == FIELD_OP:
            out.add(current["field"])
        if "arg" in current:
            collect_fields(current["arg"])
        for child in current.get("args", ()):
            collect_fields(child)
        for key in ("condition", "then", "else"):
            if key in current:
                collect_fields(current[key])

    def walk(current: dict) -> None:
        if current.get("op") == "where":
            collect_fields(current["condition"])
        if "arg" in current:
            walk(current["arg"])
        for child in current.get("args", ()):
            walk(child)
        for key in ("condition", "then", "else"):
            if key in current:
                walk(current[key])
    walk(node)
    return out


def structural_similarity(left: dict, right: dict) -> float:
    def tokens(node: dict) -> Counter:
        shaped = _canonical(node, shape=True)
        result = Counter((json.dumps(shaped, sort_keys=True,
                                     separators=(",", ":")),))
        if "arg" in node:
            result.update(tokens(node["arg"]))
        for child in node.get("args", ()):
            result.update(tokens(child))
        for key in ("condition", "then", "else"):
            if key in node:
                result.update(tokens(node[key]))
        return result
    a, b = tokens(parse(left)), tokens(parse(right))
    denominator = sum(a.values()) + sum(b.values())
    return 0.0 if not denominator else 2.0 * sum((a & b).values()) / denominator


def evaluate(samples, expr: dict) -> list[float | None]:
    """Return one causal value per sample in chronological order.

    Samples from different instruments must be evaluated separately.  This prevents
    a rolling window from leaking one instrument's event stream into another's.
    """
    node = parse(expr)
    rows = list(samples)
    if len({row.instrument_id for row in rows}) > 1:
        raise IntradayExprError("evaluate one instrument at a time")
    if any(rows[i].decision_time > rows[i + 1].decision_time
           for i in range(len(rows) - 1)):
        raise IntradayExprError("samples must be chronological")
    times = [row.decision_time for row in rows]
    timestamps = [item.timestamp() for item in times]
    cache: dict[tuple[str, int], float | bool | None] = {}
    node_keys: dict[int, str] = {}

    def key(current: dict) -> str:
        identity = id(current)
        if identity not in node_keys:
            node_keys[identity] = json.dumps(
                current, sort_keys=True, separators=(",", ":"))
        return node_keys[identity]

    def value(current: dict, index: int):
        token = (key(current), index)
        if token in cache:
            return cache[token]
        if "const" in current:
            result = float(current["const"])
        else:
            op = current["op"]
            if op == FIELD_OP:
                result = getattr(rows[index], current["field"])
            elif op in TEMPORAL_OPS:
                seconds = current["seconds"]
                cutoff = times[index].timestamp() - seconds
                if op in {"lag", "delta"}:
                    prior = bisect_right(timestamps, cutoff, hi=index + 1) - 1
                    old = value(current["arg"], prior) if prior >= 0 else None
                    now = value(current["arg"], index)
                    result = (old if op == "lag" else
                              None if old is None or now is None else float(now) - float(old))
                else:
                    lo = bisect_right(timestamps, cutoff, hi=index + 1)
                    values = [value(current["arg"], j) for j in range(lo, index + 1)]
                    values = [float(item) for item in values
                              if item is not None and not isinstance(item, bool)
                              and math.isfinite(float(item))]
                    if not values:
                        result = None
                    elif op == "rolling_mean":
                        result = fmean(values)
                    elif op == "rolling_sum":
                        result = sum(values)
                    elif op == "rolling_std":
                        result = pstdev(values) if len(values) > 1 else None
                    elif op == "rolling_zscore":
                        sd = pstdev(values) if len(values) > 1 else 0.0
                        result = ((values[-1] - fmean(values)) / sd if sd else None)
                    else:  # EWMA with half-life equal to half the declared window
                        half_life = max(1.0, seconds / 2.0)
                        js = [j for j in range(lo, index + 1)
                              if value(current["arg"], j) is not None]
                        weighted = [(float(value(current["arg"], j)),
                                     0.5 ** ((times[index] - times[j]).total_seconds()
                                             / half_life)) for j in js]
                        denominator = sum(weight for _, weight in weighted)
                        result = (sum(item * weight for item, weight in weighted)
                                  / denominator if denominator else None)
            elif op in UNARY_OPS:
                raw = value(current["arg"], index)
                if raw is None:
                    result = None
                else:
                    raw = float(raw)
                    result = {"neg": lambda: -raw, "abs": lambda: abs(raw),
                              "sign": lambda: 1.0 if raw > 0 else -1.0 if raw < 0 else 0.0,
                              "log1p_abs": lambda: math.copysign(math.log1p(abs(raw)), raw),
                              "sqrt_abs": lambda: math.copysign(math.sqrt(abs(raw)), raw)}[op]()
            elif op == "where":
                condition = value(current["condition"], index)
                result = value(current["then"] if condition else current["else"], index)
            else:
                left = value(current["args"][0], index)
                right = value(current["args"][1], index)
                if left is None or right is None:
                    result = None
                elif op == "and":
                    result = bool(left) and bool(right)
                elif op == "or":
                    result = bool(left) or bool(right)
                elif op == "gt":
                    result = float(left) > float(right)
                elif op == "lt":
                    result = float(left) < float(right)
                else:
                    left, right = float(left), float(right)
                    if op == "add": result = left + right
                    elif op == "sub": result = left - right
                    elif op == "mul": result = left * right
                    elif op == "div": result = None if right == 0 else left / right
                    elif op == "min": result = min(left, right)
                    else: result = max(left, right)
        if isinstance(result, float) and not math.isfinite(result):
            result = None
        cache[token] = result
        return result

    return [value(node, index) for index in range(len(rows))]
