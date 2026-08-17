"""Typed semantic search space shared by research and quant execution.

An LLM may propose the story, but deterministic code owns the coordinates.  The
coordinates follow the Event/Context/Qualities/Direction/Output decomposition used
by semantic alpha-search systems.  Numeric windows and thresholds deliberately do
not define an idea family: they are trials *inside* that family.
"""

from __future__ import annotations

import hashlib
import json


SEMANTIC_VERSION = "alpha-semantic-plan-v1"

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
    "L1_L10_DIVERGENCE", "CROSS_SIGNAL_INTERACTION", "STATE_CONDITIONAL",
})
DIRECTIONS = frozenset({"FOLLOW", "REVERT", "CONDITIONAL"})
OUTPUTS = frozenset({
    "FORWARD_RETURN", "MIDPRICE_MARKOUT", "TAKER_NET_PNL",
    "PASSIVE_FILL_ADJUSTED_PNL",
})
EXECUTIONS = frozenset({
    "DAILY_CLOSE_TO_OPEN", "TAKER", "PASSIVE_FIFO_LOWER_BOUND",
})

# A semantic plan need not use every related feature, but it must touch at least
# one observable that can actually represent the event.  This catches the common
# failure where an ORDER_FLOW story is implemented as a price-only formula.
EVENT_FIELDS = {
    "PRICE_TREND": frozenset({"close", "returns"}),
    "PRICE_REVERSAL": frozenset({"close", "returns"}),
    "LIQUIDITY_SHOCK": frozenset({"spread_bps", "book_depth_l1",
                                   "book_depth_l10", "trade_count"}),
    "QUOTE_IMBALANCE": frozenset({"queue_imbalance_l1", "queue_imbalance_l10",
                                   "quote_event_ofi", "normalized_quote_ofi"}),
    "ORDER_FLOW": frozenset({"trade_flow_imbalance", "quote_event_ofi",
                              "normalized_quote_ofi"}),
    "MICROPRICE_DISLOCATION": frozenset({"microprice_offset_bps"}),
    "SPREAD_CHANGE": frozenset({"spread_bps"}),
    "TRADE_BURST": frozenset({"trade_count", "trade_intensity"}),
    "VOLATILITY_BURST": frozenset({"realized_volatility_bps"}),
    "CROSS_ASSET_FLOW": frozenset({"cross_asset_flow"}),
}

CONTEXT_FIELDS = {
    "TIGHT_SPREAD": frozenset({"spread_bps"}),
    "WIDE_SPREAD": frozenset({"spread_bps"}),
    "HIGH_ACTIVITY": frozenset({"trade_count", "quote_count", "trade_intensity"}),
    "LOW_ACTIVITY": frozenset({"trade_count", "quote_count", "trade_intensity"}),
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
    if "L1_L10_DIVERGENCE" in qualities:
        has_l1 = bool(actual & {"queue_imbalance_l1", "book_depth_l1"})
        has_l10 = bool(actual & {"queue_imbalance_l10", "book_depth_l10"})
        if not (has_l1 and has_l10):
            missing.append("quality L1_L10_DIVERGENCE needs both L1 and L10 fields")
    if "CROSS_SIGNAL_INTERACTION" in qualities and (
            len(actual) < 2 or not ops & {"mul", "div", "where"}):
        missing.append(
            "quality CROSS_SIGNAL_INTERACTION needs multiple fields and an interaction operator")
    return {"ok": not missing, "fields": sorted(actual), "missing": missing}
