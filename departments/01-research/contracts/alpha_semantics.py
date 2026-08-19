"""Typed semantic search space shared by research and quant execution.

An LLM may propose the story, but deterministic code owns the coordinates.  The
coordinates follow the Event/Context/Qualities/Direction/Output decomposition used
by semantic alpha-search systems.  Numeric windows and thresholds deliberately do
not define an idea family: they are trials *inside* that family.
"""

from __future__ import annotations

import hashlib
import json


SEMANTIC_VERSION = "alpha-semantic-plan-v2"

LANES = frozenset({"DAILY_CROSS_SECTIONAL", "INTRADAY_EVENT"})
EVENTS = frozenset({
    "PRICE_TREND", "PRICE_REVERSAL", "LIQUIDITY_SHOCK",
    "QUOTE_IMBALANCE", "ORDER_FLOW", "MICROPRICE_DISLOCATION",
    "SPREAD_CHANGE", "TRADE_BURST", "VOLATILITY_BURST",
    "CROSS_ASSET_FLOW",
})
INTRADAY_EVENTS = frozenset({
    "LIQUIDITY_SHOCK", "QUOTE_IMBALANCE", "ORDER_FLOW",
    "MICROPRICE_DISLOCATION", "SPREAD_CHANGE", "TRADE_BURST",
    "VOLATILITY_BURST",
})
CONTEXTS = frozenset({
    "ALL", "OPEN", "MIDDAY", "CLOSE", "TIGHT_SPREAD", "WIDE_SPREAD",
    "HIGH_ACTIVITY", "LOW_ACTIVITY", "HIGH_VOLATILITY", "LOW_VOLATILITY",
})
QUALITIES = frozenset({
    "LEVEL", "PERSISTENCE", "ACCELERATION", "REVERSAL",
    "L1_L10_DIVERGENCE", "L1_L10_CONVERGENCE",
    "QUOTE_TAPE_CONFIRMATION", "EVENT_NORMALIZED", "VOLUME_NORMALIZED",
    "CROSS_SIGNAL_INTERACTION", "STATE_CONDITIONAL",
})
DIRECTIONS = frozenset({"FOLLOW", "REVERT", "CONDITIONAL"})
OUTPUTS = frozenset({
    "FORWARD_RETURN", "MIDPRICE_MARKOUT", "TAKER_NET_PNL",
    "PASSIVE_FILL_ADJUSTED_PNL",
})
EXECUTIONS = frozenset({
    "DAILY_CLOSE_TO_OPEN", "TAKER", "PASSIVE_FIFO_LOWER_BOUND",
})

# Observable groups are public contract data, not prompt prose.  The Scout and
# Planner render these exact groups, while intake uses them to reject a story
# whose executable AST does not contain the claimed clock/confirmation path.
# L1/L10 here means visible quote-book levels.  It does not claim MBO queue ids,
# add/cancel attribution, or an ordering between snapshots sharing one source
# timestamp.
L1_QUOTE_PRESSURE_FIELDS = frozenset({
    "queue_imbalance_l1", "quote_event_ofi", "normalized_quote_ofi",
    "normalized_quote_ofi_per_event", "quote_ofi_per_trade_volume",
})
L10_QUOTE_PRESSURE_FIELDS = frozenset({
    "queue_imbalance_l10", "multi_level_quote_ofi_l10",
    "normalized_multi_level_quote_ofi_l10",
})
EXPLICIT_L1_L10_FIELDS = frozenset({
    "depth_imbalance_slope", "quote_ofi_depth_divergence",
})
QUOTE_PRESSURE_FIELDS = frozenset(
    L1_QUOTE_PRESSURE_FIELDS | L10_QUOTE_PRESSURE_FIELDS |
    EXPLICIT_L1_L10_FIELDS)
TAPE_PRESSURE_FIELDS = frozenset({
    "trade_flow_imbalance", "signed_trade_volume",
})
EVENT_NORMALIZED_FIELDS = frozenset({"normalized_quote_ofi_per_event"})
VOLUME_NORMALIZED_FIELDS = frozenset({"quote_ofi_per_trade_volume"})

# A semantic plan need not use every related feature, but it must touch at least
# one observable that can actually represent the event.  This catches the common
# failure where an ORDER_FLOW story is implemented as a price-only formula.
EVENT_FIELDS = {
    "PRICE_TREND": frozenset({"close", "returns"}),
    "PRICE_REVERSAL": frozenset({"close", "returns"}),
    "LIQUIDITY_SHOCK": frozenset({
        "spread_bps", "book_depth_l1", "book_depth_l10", "trade_count",
        "trade_volume", "depth_imbalance_slope",
    }),
    "QUOTE_IMBALANCE": QUOTE_PRESSURE_FIELDS,
    "ORDER_FLOW": frozenset(
        QUOTE_PRESSURE_FIELDS | TAPE_PRESSURE_FIELDS | {
            "trade_side_known_ratio",
        }),
    "MICROPRICE_DISLOCATION": frozenset({"microprice_offset_bps"}),
    "SPREAD_CHANGE": frozenset({"spread_bps"}),
    "TRADE_BURST": frozenset({"trade_count", "trade_intensity", "trade_volume"}),
    "VOLATILITY_BURST": frozenset({"realized_volatility_bps"}),
    "CROSS_ASSET_FLOW": frozenset({"cross_asset_flow"}),
}

CONTEXT_FIELDS = {
    "TIGHT_SPREAD": frozenset({"spread_bps"}),
    "WIDE_SPREAD": frozenset({"spread_bps"}),
    "HIGH_ACTIVITY": frozenset({
        "trade_count", "quote_count", "trade_intensity", "trade_volume",
        "quote_event_transition_count",
    }),
    "LOW_ACTIVITY": frozenset({
        "trade_count", "quote_count", "trade_intensity", "trade_volume",
        "quote_event_transition_count",
    }),
    "HIGH_VOLATILITY": frozenset({"realized_volatility_bps"}),
    "LOW_VOLATILITY": frozenset({"realized_volatility_bps"}),
}


def _one(value, allowed: frozenset[str], field: str) -> str:
    item = str(value or "").strip().upper()
    if item not in allowed:
        raise ValueError(f"{field}={item!r} is outside {sorted(allowed)}")
    return item


def _many(value, allowed: frozenset[str], field: str) -> tuple[str, ...]:
    raw = value if isinstance(value, (list, tuple, set)) else [value]
    items = tuple(sorted({_one(item, allowed, field) for item in raw if item}))
    if not items:
        raise ValueError(f"{field} must contain at least one controlled value")
    return items


def validate(plan: dict) -> dict:
    """Validate and canonicalize a semantic plan without model judgment."""
    if not isinstance(plan, dict):
        raise ValueError("semantic_plan must be an object")
    allowed = {"event", "context", "qualities", "direction", "output",
               "execution", "horizon_seconds", "horizon_days"}
    unknown = sorted(set(plan) - allowed)
    if unknown:
        raise ValueError(f"unknown semantic_plan keys: {unknown}")

    lane_hint = "INTRADAY_EVENT" if plan.get("horizon_seconds") is not None \
        else "DAILY_CROSS_SECTIONAL"
    event = _one(plan.get("event"), EVENTS, "event")
    contexts = _many(plan.get("context") or ["ALL"], CONTEXTS, "context")
    time_contexts = set(contexts) & {"OPEN", "MIDDAY", "CLOSE"}
    if len(time_contexts) > 1:
        raise ValueError(f"mutually exclusive time contexts: {sorted(time_contexts)}")
    for left, right in (("TIGHT_SPREAD", "WIDE_SPREAD"),
                        ("HIGH_ACTIVITY", "LOW_ACTIVITY"),
                        ("HIGH_VOLATILITY", "LOW_VOLATILITY")):
        if left in contexts and right in contexts:
            raise ValueError(f"mutually exclusive contexts: {left}/{right}")
    qualities = _many(plan.get("qualities"), QUALITIES, "qualities")
    direction = _one(plan.get("direction"), DIRECTIONS, "direction")
    output = _one(plan.get("output"), OUTPUTS, "output")
    execution = _one(plan.get("execution"), EXECUTIONS, "execution")

    hs, hd = plan.get("horizon_seconds"), plan.get("horizon_days")
    if lane_hint == "INTRADAY_EVENT":
        if hd is not None:
            raise ValueError("intraday semantic plan cannot also set horizon_days")
        try:
            hs = int(hs)
        except (TypeError, ValueError):
            raise ValueError("horizon_seconds must be an integer") from None
        if not 1 <= hs <= 3600:
            raise ValueError("horizon_seconds must be in [1, 3600]")
        if output == "FORWARD_RETURN" or execution == "DAILY_CLOSE_TO_OPEN":
            raise ValueError("intraday plan needs markout/PnL output and intraday execution")
        if output == "TAKER_NET_PNL" and execution != "TAKER":
            raise ValueError("TAKER_NET_PNL requires execution=TAKER")
        if (output == "PASSIVE_FILL_ADJUSTED_PNL" and
                execution != "PASSIVE_FIFO_LOWER_BOUND"):
            raise ValueError(
                "PASSIVE_FILL_ADJUSTED_PNL requires PASSIVE_FIFO_LOWER_BOUND")
        if event not in INTRADAY_EVENTS:
            raise ValueError(
                f"event={event!r} has no executable intraday observable in {SEMANTIC_VERSION}")
        horizon = {"horizon_seconds": hs}
    else:
        if hs is not None:
            raise ValueError("daily semantic plan cannot also set horizon_seconds")
        try:
            hd = int(hd)
        except (TypeError, ValueError):
            raise ValueError("horizon_days must be an integer") from None
        if not 1 <= hd <= 250:
            raise ValueError("horizon_days must be in [1, 250]")
        if output != "FORWARD_RETURN" or execution != "DAILY_CLOSE_TO_OPEN":
            raise ValueError("daily plan needs FORWARD_RETURN/DAILY_CLOSE_TO_OPEN")
        horizon = {"horizon_days": hd}

    return {
        "event": event,
        "context": list(contexts),
        "qualities": list(qualities),
        "direction": direction,
        "output": output,
        "execution": execution,
        **horizon,
    }


def lane_of(plan: dict) -> str:
    parsed = validate(plan)
    return "INTRADAY_EVENT" if "horizon_seconds" in parsed else "DAILY_CROSS_SECTIONAL"


def family_key(plan: dict) -> dict:
    """Concept identity excluding numeric horizon/threshold tuning knobs."""
    parsed = validate(plan)
    return {key: parsed[key] for key in
            ("event", "context", "qualities", "direction", "output", "execution")}


def fingerprint(plan: dict) -> str:
    payload = json.dumps(family_key(plan), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def check_observables(plan: dict, fields, *, operators=None,
                      conditional_fields=None) -> dict:
    """Deterministic semantic-to-formula alignment check."""
    parsed = validate(plan)
    actual = {str(field) for field in fields}
    ops = {str(op) for op in (operators or ())}
    conditioned = ({str(field) for field in conditional_fields}
                   if conditional_fields is not None else None)
    event_expected = EVENT_FIELDS[parsed["event"]]
    missing = []
    if not actual & event_expected:
        missing.append(
            f"event {parsed['event']} needs one of {sorted(event_expected)}")
    for context in parsed["context"]:
        expected = CONTEXT_FIELDS.get(context)
        if expected and not actual & expected:
            missing.append(f"context {context} needs one of {sorted(expected)}")
        elif expected and conditioned is not None and not conditioned & expected:
            missing.append(
                f"context {context} must gate the signal with one of {sorted(expected)}")
    qualities = set(parsed["qualities"])
    if "PERSISTENCE" in qualities and not ops & {
            "lag", "rolling_mean", "rolling_sum", "ewma"}:
        missing.append("quality PERSISTENCE needs an explicit temporal operator")
    if "ACCELERATION" in qualities and "delta" not in ops:
        missing.append("quality ACCELERATION needs delta")
    if "STATE_CONDITIONAL" in qualities and "where" not in ops:
        missing.append("quality STATE_CONDITIONAL needs where")
    has_l1 = bool(actual & (L1_QUOTE_PRESSURE_FIELDS | {
        "book_depth_l1", "depth_imbalance_l1",
    }))
    has_l10 = bool(actual & (L10_QUOTE_PRESSURE_FIELDS | {
        "book_depth_l10", "depth_imbalance_l10",
    }))
    has_explicit_depth_relation = bool(actual & EXPLICIT_L1_L10_FIELDS)
    if "L1_L10_DIVERGENCE" in qualities and not (
            has_explicit_depth_relation or (has_l1 and has_l10)):
        missing.append(
            "quality L1_L10_DIVERGENCE needs quote_ofi_depth_divergence/"
            "depth_imbalance_slope or both L1 and L10 quote fields")
    if "L1_L10_CONVERGENCE" in qualities:
        if not (has_explicit_depth_relation or (has_l1 and has_l10)):
            missing.append(
                "quality L1_L10_CONVERGENCE needs an explicit L1/L10 relation "
                "or both L1 and L10 quote fields")
        elif not ops & {"abs", "add", "mul", "min", "max", "where"}:
            missing.append(
                "quality L1_L10_CONVERGENCE needs an explicit agreement/"
                "distance operator")
    if "QUOTE_TAPE_CONFIRMATION" in qualities:
        if not actual & QUOTE_PRESSURE_FIELDS:
            missing.append(
                "quality QUOTE_TAPE_CONFIRMATION needs a signed quote-pressure field")
        if not actual & TAPE_PRESSURE_FIELDS:
            missing.append(
                "quality QUOTE_TAPE_CONFIRMATION needs a signed tape-pressure field")
        if not ops & {"mul", "min", "max", "where"}:
            missing.append(
                "quality QUOTE_TAPE_CONFIRMATION needs an explicit confirmation gate/"
                "interaction")
    if ("EVENT_NORMALIZED" in qualities and
            not actual & EVENT_NORMALIZED_FIELDS):
        missing.append(
            "quality EVENT_NORMALIZED needs normalized_quote_ofi_per_event")
    if ("VOLUME_NORMALIZED" in qualities and
            not actual & VOLUME_NORMALIZED_FIELDS):
        missing.append(
            "quality VOLUME_NORMALIZED needs quote_ofi_per_trade_volume")
    if "CROSS_SIGNAL_INTERACTION" in qualities and (
            len(actual) < 2 or not ops & {"mul", "div", "where"}):
        missing.append(
            "quality CROSS_SIGNAL_INTERACTION needs multiple fields and an interaction operator")
    return {"ok": not missing, "fields": sorted(actual), "missing": missing}


def check_microstructure_mutations(declarations, fields, *, operators=None) -> dict:
    """Verify that clock/depth mutation labels are visible in the child AST.

    The labels guide evolutionary search but are not evidence by themselves.  A
    declaration without its required observable path would let the LLM rename a
    child while leaving the economic equation unchanged, so intake rejects it.
    Unknown declaration names remain the owning policy module's responsibility.
    """
    declared = {str(value).strip().upper() for value in (declarations or ())}
    actual = {str(field) for field in fields}
    ops = {str(op) for op in (operators or ())}
    missing: list[str] = []
    has_l1 = bool(actual & (L1_QUOTE_PRESSURE_FIELDS | {
        "book_depth_l1", "depth_imbalance_l1",
    }))
    has_l10 = bool(actual & (L10_QUOTE_PRESSURE_FIELDS | {
        "book_depth_l10", "depth_imbalance_l10",
    }))
    has_relation = bool(actual & EXPLICIT_L1_L10_FIELDS)
    for name in sorted(declared):
        if name == "L1_L10_DIVERGENCE" and not (
                has_relation or (has_l1 and has_l10)):
            missing.append(
                "L1_L10_DIVERGENCE is not visible in the candidate AST")
        elif name == "L1_L10_CONVERGENCE":
            if not (has_relation or (has_l1 and has_l10)):
                missing.append(
                    "L1_L10_CONVERGENCE is missing its L1/L10 observable path")
            elif not ops & {"abs", "add", "mul", "min", "max", "where"}:
                missing.append(
                    "L1_L10_CONVERGENCE is missing an agreement/distance operator")
        elif name == "QUOTE_TAPE_CONFIRMATION":
            if not actual & QUOTE_PRESSURE_FIELDS or not actual & TAPE_PRESSURE_FIELDS:
                missing.append(
                    "QUOTE_TAPE_CONFIRMATION needs signed quote and tape pressure")
            elif not ops & {"mul", "min", "max", "where"}:
                missing.append(
                    "QUOTE_TAPE_CONFIRMATION needs a confirmation gate/interaction")
        elif name == "EVENT_NORMALIZATION" and not (
                actual & EVENT_NORMALIZED_FIELDS):
            missing.append(
                "EVENT_NORMALIZATION needs normalized_quote_ofi_per_event")
        elif name == "VOLUME_NORMALIZATION" and not (
                actual & VOLUME_NORMALIZED_FIELDS):
            missing.append(
                "VOLUME_NORMALIZATION needs quote_ofi_per_trade_volume")
    return {"ok": not missing, "fields": sorted(actual), "missing": missing}
