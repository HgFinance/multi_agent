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


AST_VERSION = "intraday-alpha-ast-v2"
LEGACY_FEATURE_WINDOW_CONTRACT = "legacy-cohort-lookback-v1"
EXPLICIT_FEATURE_WINDOW_CONTRACT = "explicit-primitive-window-v2"
FEATURE_WINDOW_CONTRACTS = frozenset({
    LEGACY_FEATURE_WINDOW_CONTRACT,
    EXPLICIT_FEATURE_WINDOW_CONTRACT,
})
PRIMITIVE_WINDOWS_SECONDS = (2, 5, 10, 30, 60, 300, 600)
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
    "multi_level_quote_ofi_l10": SHARES,
    "normalized_multi_level_quote_ofi_l10": RATIO,
    "depth_imbalance_slope": RATIO,
    "quote_ofi_depth_divergence": RATIO,
    "quote_event_transition_count": COUNT,
    "normalized_quote_ofi_per_event": RATIO,
    "signed_trade_volume": SHARES,
    "trade_volume": SHARES,
    "trade_side_known_ratio": RATIO,
    "quote_ofi_per_trade_volume": RATIO,
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

# A state field describes the book at the decision snapshot.  A windowed field
# describes raw events in a completed-second interval ending at that snapshot.
# Keeping those two namespaces explicit prevents a cohort sidecar's temporal
# clock from silently changing the economic meaning of a primitive field.
WINDOWED_FIELDS = frozenset({
    "trade_flow_imbalance",
    "quote_event_ofi",
    "normalized_quote_ofi",
    "multi_level_quote_ofi_l10",
    "normalized_multi_level_quote_ofi_l10",
    "quote_ofi_depth_divergence",
    "quote_event_transition_count",
    "normalized_quote_ofi_per_event",
    "signed_trade_volume",
    "trade_volume",
    "trade_side_known_ratio",
    "quote_ofi_per_trade_volume",
    "trade_count",
    "quote_count",
    "trade_intensity",
    "realized_volatility_bps",
})
STATE_FIELDS = frozenset(FIELDS) - WINDOWED_FIELDS

# ``parse`` is the local strict grammar and deliberately remains a superset of
# the currently replayable external history.  The 61-session historical lane is
# reconstructed from completed-second, order-ambiguous snapshots: it can
# observe book state and printed trades, but it cannot recover the within-second
# quote transition sequence required by event OFI.  Keep those fields available
# for a future sequenced feed while rejecting them deterministically at the
# current screening-cohort boundary.
COMPLETED_SECOND_SCREENING_COHORT_VERSION = "intraday-screening-cohort-v4"
COMPLETED_SECOND_ALLOWED_EXECUTIONS = frozenset({"TAKER"})
COMPLETED_SECOND_SEQUENCE_DEPENDENT_FIELDS = frozenset({
    "quote_event_ofi",
    "normalized_quote_ofi",
    "multi_level_quote_ofi_l10",
    "normalized_multi_level_quote_ofi_l10",
    "quote_ofi_depth_divergence",
    "quote_event_transition_count",
    "normalized_quote_ofi_per_event",
    "quote_ofi_per_trade_volume",
})
COMPLETED_SECOND_RECOMMENDED_FIELDS = frozenset({
    "queue_imbalance_l1",
    "queue_imbalance_l10",
    "microprice_offset_bps",
    "depth_imbalance_slope",
    "trade_flow_imbalance",
    "signed_trade_volume",
    "trade_volume",
    "trade_side_known_ratio",
    "spread_bps",
    "bid_depth_l1",
    "ask_depth_l1",
    "book_depth_l1",
    "book_depth_l10",
    "trade_count",
    "quote_count",
    "trade_intensity",
    "realized_volatility_bps",
    "quote_age_ms",
})
COMPLETED_SECOND_REPLAYABLE_FIELDS = (
    frozenset(FIELDS) - COMPLETED_SECOND_SEQUENCE_DEPENDENT_FIELDS
)

FIELD_OP = "field"
LOWER_PREDICATE_POLARITY = "LOWER"
UPPER_PREDICATE_POLARITY = "UPPER"
PREDICATE_POLARITIES = frozenset({
    LOWER_PREDICATE_POLARITY,
    UPPER_PREDICATE_POLARITY,
})
MAX_SIGNAL_SUPPORT_PATHS = 256
TEMPORAL_OPS = frozenset({"lag", "rolling_mean", "rolling_std", "rolling_sum",
                          "delta", "ewma", "rolling_zscore"})
UNARY_OPS = frozenset({"neg", "abs", "sign", "log1p_abs", "sqrt_abs"})
BINARY_OPS = frozenset({"add", "sub", "mul", "div", "min", "max", "gt", "lt",
                        "and", "or"})
TERNARY_OPS = frozenset({"where"})
ALL_OPS = frozenset({FIELD_OP}) | TEMPORAL_OPS | UNARY_OPS | BINARY_OPS | TERNARY_OPS

WALL_TIME_CLOCK = "WALL_TIME_SECONDS"
QUOTE_EVENT_CLOCK = "QUOTE_SNAPSHOT_EVENT"
TRADE_VOLUME_CLOCK = "PRINTED_TRADE_VOLUME"
DECISION_SNAPSHOT_CLOCK = "DECISION_SNAPSHOT"
QUOTE_EVENT_CLOCK_FIELDS = frozenset({
    "quote_event_ofi", "normalized_quote_ofi",
    "multi_level_quote_ofi_l10", "normalized_multi_level_quote_ofi_l10",
    "quote_ofi_depth_divergence", "quote_event_transition_count",
    "normalized_quote_ofi_per_event", "quote_count",
})
TRADE_VOLUME_CLOCK_FIELDS = frozenset({
    "trade_flow_imbalance", "signed_trade_volume", "trade_volume",
    "trade_side_known_ratio", "quote_ofi_per_trade_volume", "trade_count",
})

# A non-negative data-quality ratio can attenuate or gate a signed pressure,
# but it cannot identify the direction of a future markout by itself.  Keep
# this executable invariant in the shared contract so a formula admitted by an
# older research image is still checked immediately before current-V2 replay.
NONNEGATIVE_DIRECTIONAL_QUALITY_FIELDS = frozenset({
    "trade_side_known_ratio",
})
DIRECTIONAL_PRESSURE_FIELDS = frozenset({
    "queue_imbalance_l1", "queue_imbalance_l10", "microprice_offset_bps",
    "trade_flow_imbalance", "quote_event_ofi", "normalized_quote_ofi",
    "multi_level_quote_ofi_l10", "normalized_multi_level_quote_ofi_l10",
    "depth_imbalance_slope", "quote_ofi_depth_divergence",
    "normalized_quote_ofi_per_event", "signed_trade_volume",
    "quote_ofi_per_trade_volume",
})
DIRECTIONAL_QUALITY_PATH_CONTRACT_VERSION = \
    "nonnegative-directional-quality-path-v1"


class IntradayExprError(ValueError):
    pass


def _unknown(node: dict, allowed: set[str], kind: str) -> None:
    extra = sorted(set(node) - allowed)
    if extra:
        raise IntradayExprError(f"unknown {kind} keys: {extra}")


def _seconds(node: dict) -> int:
    raw = node.get("seconds")
    if (isinstance(raw, bool) or not isinstance(raw, (int, float))
            or not math.isfinite(float(raw)) or not float(raw).is_integer()):
        raise IntradayExprError(f"seconds must be an integer: {raw!r}") from None
    value = int(raw)
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
        _unknown(node, {"op", "field", "seconds"}, op)
        field = str(node.get("field") or "")
        if field not in FIELDS:
            raise IntradayExprError(f"unknown intraday field {field!r}")
        parsed = {"op": op, "field": field}
        if "seconds" in node:
            parsed["seconds"] = _seconds(node)
        return parsed
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
        out = {"op": FIELD_OP, "field": node["field"]}
        if "seconds" in node:
            out["seconds"] = "#" if shape else node["seconds"]
        return out
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


def primitive_windows_of(expr: dict) -> set[int]:
    """Return raw-event primitive windows, excluding AST temporal clocks."""
    node = parse(expr)
    out: set[int] = set()

    def walk(current: dict) -> None:
        if current.get("op") == FIELD_OP and "seconds" in current:
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


def temporal_windows_of(expr: dict) -> set[int]:
    """Return evaluator windows, excluding raw-event primitive windows."""
    node = parse(expr)
    out: set[int] = set()

    def walk(current: dict) -> None:
        if current.get("op") in TEMPORAL_OPS:
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


def field_window_bindings_of(expr: dict) -> tuple[tuple[str, int | None], ...]:
    """Return deterministic primitive bindings used by identity/manifests."""
    node = parse(expr)
    bindings: set[tuple[str, int | None]] = set()

    def walk(current: dict) -> None:
        if current.get("op") == FIELD_OP:
            bindings.add((current["field"], current.get("seconds")))
        if "arg" in current:
            walk(current["arg"])
        for child in current.get("args", ()):
            walk(child)
        for key in ("condition", "then", "else"):
            if key in current:
                walk(current[key])
    walk(node)
    return tuple(sorted(bindings, key=lambda item: (item[0], item[1] or 0)))


def validate_feature_window_contract(
        expr: dict, *, contract_version: str) -> dict:
    """Validate primitive leaf semantics without changing the legacy grammar.

    ``parse`` intentionally remains backward compatible: historical V1/v11
    candidates contain bare windowed fields whose window was supplied by the
    cohort-wide lane spec.  V2 candidates must bind every windowed primitive to
    one member of :data:`PRIMITIVE_WINDOWS_SECONDS`; decision-snapshot fields
    must remain unwindowed.
    """
    parsed = parse(expr)
    version = str(contract_version or "").strip()
    if version not in FEATURE_WINDOW_CONTRACTS:
        raise IntradayExprError(f"unknown feature-window contract {version!r}")

    def walk(current: dict) -> None:
        if current.get("op") == FIELD_OP:
            field = current["field"]
            seconds = current.get("seconds")
            if version == LEGACY_FEATURE_WINDOW_CONTRACT:
                if seconds is not None:
                    raise IntradayExprError(
                        "legacy feature-window leaves must not declare seconds")
            elif field in WINDOWED_FIELDS:
                if seconds is None:
                    raise IntradayExprError(
                        f"windowed field {field!r} requires explicit seconds")
                if seconds not in PRIMITIVE_WINDOWS_SECONDS:
                    allowed = ", ".join(map(str, PRIMITIVE_WINDOWS_SECONDS))
                    raise IntradayExprError(
                        f"field {field!r} seconds={seconds} is not in {{{allowed}}}")
            elif seconds is not None:
                raise IntradayExprError(
                    f"decision-snapshot field {field!r} cannot declare seconds")
        if "arg" in current:
            walk(current["arg"])
        for child in current.get("args", ()):
            walk(child)
        for key in ("condition", "then", "else"):
            if key in current:
                walk(current[key])
    walk(parsed)
    return parsed


def validate_completed_second_candidate(
        expr: dict, *, execution: str) -> dict:
    """Validate a candidate against the current external-history capability.

    This is intentionally *not* part of :func:`parse`: local or future feeds
    with exchange sequence can still use the complete strict grammar.  Intake
    calls this narrower gate only for ``intraday-screening-cohort-v4`` shared
    replay, whose within-second quote snapshots are an unordered multiset and
    whose historical fill model is taker-only.
    """
    parsed = parse(expr)
    execution_name = str(execution or "").strip().upper()
    if execution_name not in COMPLETED_SECOND_ALLOWED_EXECUTIONS:
        allowed = ", ".join(sorted(COMPLETED_SECOND_ALLOWED_EXECUTIONS))
        raise IntradayExprError(
            f"{COMPLETED_SECOND_SCREENING_COHORT_VERSION} completed-second "
            f"external history is {allowed} only; got execution="
            f"{execution_name or '<missing>'!r}")
    blocked = sorted(fields_of(parsed) & COMPLETED_SECOND_SEQUENCE_DEPENDENT_FIELDS)
    if blocked:
        raise IntradayExprError(
            f"{COMPLETED_SECOND_SCREENING_COHORT_VERSION} completed-second "
            "external history has no deterministic within-second quote "
            f"sequence; blocked fields: {', '.join(blocked)}")
    return parsed


def validate_directional_quality_paths(expr: dict) -> dict:
    """Reject non-negative quality terms used as directional intercepts.

    When an expression contains both a signed pressure and a non-negative
    quality observable, every numeric quality occurrence must either be in a
    ``where`` condition or be multiplied by a distinct pressure subtree.  This
    permits economically meaningful gates and attenuation while rejecting, for
    example, ``add(trade_flow_imbalance, trade_side_known_ratio)``.  A
    quality-only structural ablation remains legal; its lack of promotion
    authority is governed by the screening-cohort contract.
    """
    parsed = parse(expr)
    present = fields_of(parsed)
    quality_fields = present & NONNEGATIVE_DIRECTIONAL_QUALITY_FIELDS
    pressure_fields = present & DIRECTIONAL_PRESSURE_FIELDS
    if not quality_fields or not pressure_fields:
        return parsed

    def subtree_fields(node: dict) -> set[str]:
        if node.get("op") == FIELD_OP:
            return {str(node["field"])}
        found: set[str] = set()
        if isinstance(node.get("arg"), dict):
            found.update(subtree_fields(node["arg"]))
        for child in node.get("args") or ():
            if isinstance(child, dict):
                found.update(subtree_fields(child))
        for key in ("condition", "then", "else"):
            if isinstance(node.get(key), dict):
                found.update(subtree_fields(node[key]))
        return found

    unsupported: set[str] = set()

    def walk(node: dict, *, gate: bool, pressure_coupled: bool) -> None:
        op = node.get("op")
        if op == FIELD_OP:
            field = str(node["field"])
            if (field in quality_fields and not gate
                    and not pressure_coupled):
                unsupported.add(field)
            return
        if op == "where":
            walk(node["condition"], gate=True,
                 pressure_coupled=pressure_coupled)
            walk(node["then"], gate=gate,
                 pressure_coupled=pressure_coupled)
            walk(node["else"], gate=gate,
                 pressure_coupled=pressure_coupled)
            return
        if isinstance(node.get("arg"), dict):
            walk(node["arg"], gate=gate,
                 pressure_coupled=pressure_coupled)
            return

        children = [child for child in node.get("args") or ()
                    if isinstance(child, dict)]
        child_fields = [subtree_fields(child) for child in children]
        for index, child in enumerate(children):
            sibling_has_pressure = (
                op == "mul" and any(
                    child_fields[other] & pressure_fields
                    for other in range(len(children)) if other != index
                )
            )
            walk(
                child,
                gate=gate,
                pressure_coupled=(pressure_coupled or sibling_has_pressure),
            )

    walk(parsed, gate=False, pressure_coupled=False)
    if unsupported:
        raise IntradayExprError(
            f"{DIRECTIONAL_QUALITY_PATH_CONTRACT_VERSION}: non-negative "
            "directional quality fields must gate or multiply a distinct "
            "signed pressure subtree; additive/min/max numeric paths create "
            f"a directional intercept: {sorted(unsupported)}")
    return parsed


def clock_domains_of(expr: dict) -> set[str]:
    """Return the physical clocks represented by an expression.

    The evaluator has exact wall-time windows over decision samples.  Event and
    volume clocks are deliberately exposed as causal raw-window summaries, not
    as fake ``N sample rows == N exchange events`` temporal operators.  This
    distinction is material for the external feed whose timestamp is only
    second-resolution and has no MBO sequence.
    """
    node = parse(expr)
    domains: set[str] = set()
    used_fields = fields_of(node)
    if used_fields & QUOTE_EVENT_CLOCK_FIELDS:
        domains.add(QUOTE_EVENT_CLOCK)
    if used_fields & TRADE_VOLUME_CLOCK_FIELDS:
        domains.add(TRADE_VOLUME_CLOCK)
    if any(operator in TEMPORAL_OPS for operator in operators_of(node)):
        domains.add(WALL_TIME_CLOCK)
    return domains


def effective_clock_domains_of(expr: dict) -> set[str]:
    """Return a non-empty behavior clock for search/archive coordinates.

    A level-only formula has no rolling, event-count, or volume window.  It is
    nevertheless evaluated causally at the current decision snapshot.  Keeping
    that coordinate explicit avoids conflating an instantaneous state with the
    separate EVENT_TIME_HISTORICAL_ONLY knowledge-clock policy.
    """
    domains = clock_domains_of(expr)
    return domains or {DECISION_SNAPSHOT_CLOCK}


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


def signal_support_predicate_paths_of(
        expr: dict) -> tuple[frozenset[tuple[str, str]], ...]:
    """Return predicate paths under which the numeric signal may be non-zero.

    Each item is one conjunction of normalized ``(field, polarity)`` literals;
    the items together are alternatives.  A directional context is therefore
    valid only when every possible non-zero output path guarantees it.  This
    makes output-path dominance explicit instead of collecting fields mentioned
    by an arbitrary nested selector.

    Direct field/constant ``gt`` and ``lt`` comparisons are normalized from the
    field's point of view, including reversed operands.  False branches invert
    the polarity.  Boolean ``and``/``or`` and boolean-valued nested ``where``
    nodes are expanded into truth paths, so alternative activity fields remain
    separate valid paths while unrelated or inverted branches cannot borrow a
    predicate from one another.

    Arithmetic support is conservative.  Additive alternatives are unioned,
    multiplicative requirements are intersected, and a temporal operator over
    an internally gated value loses the current-snapshot predicate because its
    history may stay non-zero after that predicate turns false.  Unsupported or
    over-complex analysis becomes an unconstrained path and therefore fails
    closed unless an analyzable outer gate dominates it.
    """
    node = parse(expr)
    empty_path: frozenset[tuple[str, str]] = frozenset()

    def path_key(path: frozenset[tuple[str, str]]) -> tuple:
        return len(path), tuple(sorted(path))

    def normalize(paths) -> tuple[frozenset[tuple[str, str]], ...]:
        kept: list[frozenset[tuple[str, str]]] = []
        for path in sorted(set(paths), key=path_key):
            # In a union, a less constrained path subsumes a stricter one for
            # proving a context that must dominate every alternative.
            if any(existing.issubset(path) for existing in kept):
                continue
            kept.append(path)
            if len(kept) > MAX_SIGNAL_SUPPORT_PATHS:
                return (empty_path,)
        return tuple(kept)

    def union(left, right) -> tuple[frozenset[tuple[str, str]], ...]:
        return normalize((*left, *right))

    def combine(left, right) -> tuple[frozenset[tuple[str, str]], ...]:
        if not left or not right:
            return ()
        combined = []
        for left_path in left:
            for right_path in right:
                combined.append(frozenset(left_path | right_path))
                if len(combined) > MAX_SIGNAL_SUPPORT_PATHS * 4:
                    return (empty_path,)
        return normalize(combined)

    def comparison_literal(current: dict) -> tuple[str, str] | None:
        op = current.get("op")
        if op not in {"gt", "lt"}:
            return None
        left, right = current["args"]
        if left.get("op") == FIELD_OP and "const" in right:
            polarity = (LOWER_PREDICATE_POLARITY if op == "lt"
                        else UPPER_PREDICATE_POLARITY)
            return left["field"], polarity
        if "const" in left and right.get("op") == FIELD_OP:
            polarity = (UPPER_PREDICATE_POLARITY if op == "lt"
                        else LOWER_PREDICATE_POLARITY)
            return right["field"], polarity
        return None

    def inverted(literal: tuple[str, str]) -> tuple[str, str]:
        field, polarity = literal
        inverse = (UPPER_PREDICATE_POLARITY
                   if polarity == LOWER_PREDICATE_POLARITY
                   else LOWER_PREDICATE_POLARITY)
        return field, inverse

    def boolean_paths(current: dict):
        literal = comparison_literal(current)
        if literal is not None:
            return ((frozenset({literal}),),
                    (frozenset({inverted(literal)}),))

        op = current.get("op")
        if op in {"and", "or"}:
            left_true, left_false = boolean_paths(current["args"][0])
            right_true, right_false = boolean_paths(current["args"][1])
            if op == "and":
                return (combine(left_true, right_true),
                        union(left_false, right_false))
            return (union(left_true, right_true),
                    combine(left_false, right_false))
        if op == "where":
            selector_true, selector_false = boolean_paths(current["condition"])
            then_true, then_false = boolean_paths(current["then"])
            else_true, else_false = boolean_paths(current["else"])
            true_paths = union(
                combine(selector_true, then_true),
                combine(selector_false, else_true),
            )
            false_paths = union(
                combine(selector_true, then_false),
                combine(selector_false, else_false),
            )
            return true_paths, false_paths

        # The grammar guarantees BOOL here, but a transformed comparison has no
        # controlled direct-field polarity.  Either truth value remains possible.
        return (empty_path,), (empty_path,)

    def support_paths(current: dict):
        if "const" in current:
            return () if float(current["const"]) == 0.0 else (empty_path,)

        op = current.get("op")
        if op == FIELD_OP:
            return (empty_path,)
        if op in TEMPORAL_OPS:
            # A prior gated observation can keep a temporal value non-zero after
            # the current context turns false.  Preserve only exact zero-ness.
            return () if not support_paths(current["arg"]) else (empty_path,)
        if op in UNARY_OPS:
            return support_paths(current["arg"])
        if op == "where":
            true_paths, false_paths = boolean_paths(current["condition"])
            return union(
                combine(true_paths, support_paths(current["then"])),
                combine(false_paths, support_paths(current["else"])),
            )
        if op in {"add", "sub", "min", "max"}:
            return union(support_paths(current["args"][0]),
                         support_paths(current["args"][1]))
        if op in {"mul", "div"}:
            return combine(support_paths(current["args"][0]),
                           support_paths(current["args"][1]))

        # A future numeric operator is not proof of context dominance.
        return (empty_path,)

    return normalize(support_paths(node))


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


def evaluate(samples, expr: dict, *, feature_cube=None,
             feature_window_contract: str | None = None) -> list[float | None]:
    """Return one causal value per sample in chronological order.

    Samples from different instruments must be evaluated separately.  This prevents
    a rolling window from leaking one instrument's event stream into another's.
    """
    node = (validate_feature_window_contract(
        expr, contract_version=feature_window_contract)
        if feature_window_contract is not None else parse(expr))
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
                if "seconds" not in current:
                    result = getattr(rows[index], current["field"])
                elif feature_cube is None:
                    raise IntradayExprError(
                        "explicit-window field evaluation requires a feature cube")
                else:
                    result = feature_cube.value(
                        current["field"], int(current["seconds"]), index)
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
