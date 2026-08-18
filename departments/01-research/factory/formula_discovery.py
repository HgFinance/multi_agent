"""Typed financial-mathematics contract for LLM-proposed intraday formulas.

The LLM owns the scientific prior and proposes an equation skeleton.  Code owns
syntax, units, semantic alignment, complexity, and whether the claimed
functional form is actually visible in the AST.  Numeric OOS fitting never
occurs here; empirical survival remains the quant evaluator's job.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError


CONTRACT_VERSION = "formula-discovery-v5"
CALIBRATION_CONTRACT = "ORIGIN_ANCHORED_POSITIVE_SHRINKAGE_V1"

# These names are the canonical ``alpha_semantics.OUTPUTS`` values.  Keeping a
# second set of near-synonyms here used to make gross-markout and passive
# formulas impossible to submit: the thesis had to equal SEMANTIC_PLAN.output,
# while the two validators accepted different spellings.
TARGETS = frozenset({
    "MIDPRICE_MARKOUT", "TAKER_NET_PNL", "PASSIVE_FILL_ADJUSTED_PNL",
})
FUNCTIONAL_FORMS = frozenset({
    "MONOTONE", "REVERSAL", "INTERACTION", "STATE_CONDITIONAL",
    "CROSS_SCALE", "DEPTH_DIVERGENCE", "L1_L10_CONFIRMATION",
    "QUOTE_TAPE_CONFIRMATION", "CLOCK_NORMALIZED",
})
COEFFICIENT_POLICIES = frozenset({
    "FIXED_FROM_SOURCE", "PREREGISTERED_NO_OOS_FIT", "STRUCTURE_ONLY",
})
EXPECTED_SIGNS = frozenset({"POSITIVE", "NEGATIVE", "STATE_DEPENDENT"})
DECISION_RULES = frozenset({
    "POSITIVE_SCORE",
    "PREDICTED_MARKOUT_CLEARS_COST",
})
TERM_ROLES = frozenset({
    "PRESSURE", "LIQUIDITY", "STATE", "SCALE", "VOLATILITY",
    "FRESHNESS", "ACTIVITY", "CAPACITY", "CONFIRMATION",
    "NORMALIZER", "DEPTH_SHAPE",
})

# These observables cannot be negative by construction.  Applying ``sign`` to
# one of them (or to a non-negative rolling transform of it) discards magnitude
# and normally collapses to the constant +1.  That is especially dangerous when
# an LLM puts the result in a denominator merely to make a field appear in the
# equation/thesis.  A genuine presence state remains expressible and auditable
# as an explicit ``where(gt(...))`` gate.
_NONNEGATIVE_FIELDS = frozenset({
    "spread_bps", "bid_depth_l1", "ask_depth_l1", "book_depth_l1",
    "book_depth_l10", "trade_count", "quote_count", "trade_intensity",
    "realized_volatility_bps", "quote_age_ms", "quote_event_transition_count",
    "trade_volume", "trade_side_known_ratio",
})
_ALWAYS_NONNEGATIVE_OPS = frozenset({"abs", "rolling_std"})
_NONNEGATIVE_PRESERVING_OPS = frozenset({
    "lag", "rolling_mean", "rolling_sum", "ewma",
    "log1p_abs", "sqrt_abs", "sign",
})

# Signed supply/demand observables that can identify the direction of a future
# price markout.  Activity, spread, depth and realised volatility may condition
# or scale an edge, but their level alone predicts magnitude/state rather than
# whether the next price move is up or down.  Requiring a VALUE path (not merely
# a where gate) also prevents ``high activity -> always buy`` from masquerading
# as a directional microstructure equation.
_DIRECTIONAL_PRESSURE_FIELDS = frozenset({
    "queue_imbalance_l1", "queue_imbalance_l10", "microprice_offset_bps",
    "trade_flow_imbalance", "quote_event_ofi", "normalized_quote_ofi",
    "multi_level_quote_ofi_l10", "normalized_multi_level_quote_ofi_l10",
    "depth_imbalance_slope", "quote_ofi_depth_divergence",
    "normalized_quote_ofi_per_event", "signed_trade_volume",
    "quote_ofi_per_trade_volume",
})
DIRECTIONAL_PRESSURE_FIELDS = _DIRECTIONAL_PRESSURE_FIELDS

_L1_QUOTE_FIELDS = frozenset({
    "queue_imbalance_l1", "quote_event_ofi", "normalized_quote_ofi",
    "normalized_quote_ofi_per_event", "quote_ofi_per_trade_volume",
})
_L10_QUOTE_FIELDS = frozenset({
    "queue_imbalance_l10", "multi_level_quote_ofi_l10",
    "normalized_multi_level_quote_ofi_l10",
})
_EXPLICIT_DEPTH_RELATION_FIELDS = frozenset({
    "depth_imbalance_slope", "quote_ofi_depth_divergence",
})
_QUOTE_PRESSURE_FIELDS = frozenset(
    _L1_QUOTE_FIELDS | _L10_QUOTE_FIELDS | _EXPLICIT_DEPTH_RELATION_FIELDS)
_TAPE_PRESSURE_FIELDS = frozenset({
    "trade_flow_imbalance", "signed_trade_volume",
})
_CLOCK_NORMALIZED_FIELDS = frozenset({
    "normalized_quote_ofi_per_event", "quote_ofi_per_trade_volume",
})


class FormulaThesisV1(BaseModel):
    """Decoder/runtime schema; AST-dependent invariants are checked by ``assess``."""

    model_config = ConfigDict(extra="forbid")

    target: str
    functional_form: str
    expected_sign: str
    coefficient_policy: str
    decision_rule: str
    terms: dict[str, str]
    identification: str = Field(min_length=20)


def _text(value, name: str) -> str:
    result = str(value or "").strip().upper()
    if not result:
        raise ValueError(f"FORMULA_THESIS.{name} is required")
    return result


def _canonical(node: dict) -> str:
    return json.dumps(node, sort_keys=True, separators=(",", ":"))


def _guaranteed_nonnegative(node: dict) -> bool:
    """Conservative symbolic domain analysis; false means unknown, not negative."""
    if "const" in node:
        return float(node["const"]) >= 0.0
    op = node.get("op")
    if op == "field":
        return node.get("field") in _NONNEGATIVE_FIELDS
    if op in _ALWAYS_NONNEGATIVE_OPS:
        return True
    if op in _NONNEGATIVE_PRESERVING_OPS:
        return _guaranteed_nonnegative(node["arg"])
    if op in {"add", "mul", "min", "max"}:
        return all(_guaranteed_nonnegative(arg) for arg in node["args"])
    if op == "div":
        return all(_guaranteed_nonnegative(arg) for arg in node["args"])
    if op == "where":
        return (_guaranteed_nonnegative(node["then"])
                and _guaranteed_nonnegative(node["else"]))
    return False


def _symbolic_degeneracies(candidate: dict) -> list[str]:
    """Find exact algebraic identities that make an advertised term decorative."""
    out: list[str] = []

    def walk(node: dict) -> None:
        op = node.get("op")
        if op == "where":
            if _canonical(node["then"]) == _canonical(node["else"]):
                out.append("where has identical then/else branches")
            walk(node["condition"])
            walk(node["then"])
            walk(node["else"])
            return
        if "arg" in node:
            walk(node["arg"])
            return
        args = node.get("args") or ()
        if len(args) == 2:
            same = _canonical(args[0]) == _canonical(args[1])
            if same and op == "sub":
                out.append("sub uses the same expression twice and collapses to zero")
            elif same and op == "div":
                out.append("div uses the same expression twice and collapses to one")
            elif same and op in {"min", "max"}:
                out.append(f"{op} repeats the same expression")
            if op == "mul" and any(
                    "const" in arg and float(arg["const"]) == 0.0
                    for arg in args):
                out.append("mul by zero erases the other term")
            for arg in args:
                walk(arg)

    walk(candidate)
    return list(dict.fromkeys(out))


def _term_influence(candidate: dict, fields: list[str]) -> dict[str, list[str]]:
    """Classify how each declared observable can affect the executable score.

    This is deliberately structural rather than fitted: ``VALUE`` preserves a
    numeric path, ``GATE`` controls a where branch, and ``PRESENCE_ONLY`` is a
    lossy sign transform of an observable known to be non-negative.  A term that
    appears only through the last path is not an identified equation term.
    """
    modes = {field: set() for field in fields}

    def walk(node: dict, mode: str = "VALUE") -> None:
        op = node.get("op")
        if op == "field":
            modes[node["field"]].add(mode)
            return
        if op == "where":
            walk(node["condition"], "GATE")
            walk(node["then"], mode)
            walk(node["else"], mode)
            return
        if "arg" in node:
            child_mode = mode
            if (mode != "GATE" and op == "sign"
                    and _guaranteed_nonnegative(node["arg"])):
                child_mode = "PRESENCE_ONLY"
            walk(node["arg"], child_mode)
            return
        for child in node.get("args") or ():
            walk(child, mode)

    walk(candidate)
    return {field: sorted(value) for field, value in sorted(modes.items())}


def assess(thesis, *, candidate: dict, semantic_plan: dict, grammar) -> dict:
    """Validate an LLM equation thesis against the executable AST."""
    clock_domains = (sorted(grammar.clock_domains_of(candidate))
                     if hasattr(grammar, "clock_domains_of") else [])
    profile = {
        "grammar_version": str(getattr(grammar, "AST_VERSION", "")),
        "fields": sorted(grammar.fields_of(candidate)),
        "operators": sorted(grammar.operators_of(candidate)),
        "clocks_seconds": sorted(grammar.clocks_of(candidate)),
        "clock_domains": clock_domains,
        "complexity_nodes": grammar.count_nodes(candidate),
        "output_unit": grammar.unit_of(candidate),
    }
    if thesis in (None, ""):
        raise ValueError("FORMULA_THESIS is required for an intraday AST_READY lead")
    if not isinstance(thesis, dict):
        raise ValueError("FORMULA_THESIS must be a JSON object")
    try:
        thesis = FormulaThesisV1.model_validate(thesis).model_dump()
    except ValidationError as exc:
        raise ValueError(f"FORMULA_THESIS schema violation: {exc}") from exc

    target = _text(thesis.get("target"), "target")
    form = _text(thesis.get("functional_form"), "functional_form")
    sign = _text(thesis.get("expected_sign"), "expected_sign")
    coefficient_policy = _text(
        thesis.get("coefficient_policy"), "coefficient_policy")
    decision_rule = _text(thesis.get("decision_rule"), "decision_rule")
    for value, allowed, name in (
        (target, TARGETS, "target"),
        (form, FUNCTIONAL_FORMS, "functional_form"),
        (sign, EXPECTED_SIGNS, "expected_sign"),
        (coefficient_policy, COEFFICIENT_POLICIES, "coefficient_policy"),
        (decision_rule, DECISION_RULES, "decision_rule"),
    ):
        if value not in allowed:
            raise ValueError(f"FORMULA_THESIS.{name}={value!r} is not controlled")
    if target != str(semantic_plan.get("output") or "").upper():
        raise ValueError("FORMULA_THESIS.target must equal SEMANTIC_PLAN.output")

    # A fixed/preregistered equation that claims to predict markout must already
    # be dimensioned in BPS.  A STRUCTURE_ONLY equation is deliberately
    # different: it describes the economic shape and direction, while a
    # deterministic calibration-only mapper estimates one positive, shrunken
    # score->BPS coefficient before the OOS sessions.  This prevents the LLM
    # from multiplying a useful pressure score by spread/volatility merely to
    # satisfy units and then having that arbitrary number treated as a calibrated
    # future markout.
    structure_only = coefficient_policy == "STRUCTURE_ONLY"
    if not structure_only and profile["output_unit"] != "BPS":
        raise ValueError(
            "fixed/preregistered FORMULA_THESIS markout equations must output "
            f"BPS, not {profile['output_unit']}. REPAIR: either provide a "
            "source-identified BPS equation or set coefficient_policy="
            "STRUCTURE_ONLY so the runtime estimates one locked score-to-BPS "
            "coefficient on calibration sessions only; never add a BPS field "
            "only to satisfy dimensions")
    if structure_only and profile["output_unit"] == "BOOL":
        raise ValueError("STRUCTURE_ONLY AST must output a numeric signed score")
    pnl_target = target in {
        "TAKER_NET_PNL", "PASSIVE_FILL_ADJUSTED_PNL",
    }
    if pnl_target and decision_rule != "PREDICTED_MARKOUT_CLEARS_COST":
        raise ValueError(
            "net-PnL targets require decision_rule="
            "PREDICTED_MARKOUT_CLEARS_COST")
    execution = str(semantic_plan.get("execution") or "").upper()
    expected_execution = {
        "TAKER_NET_PNL": "TAKER",
        "PASSIVE_FILL_ADJUSTED_PNL": "PASSIVE_FIFO_LOWER_BOUND",
    }.get(target)
    if expected_execution and execution != expected_execution:
        raise ValueError(
            f"FORMULA_THESIS.target={target} requires "
            f"SEMANTIC_PLAN.execution={expected_execution}")

    raw_terms = thesis.get("terms")
    if not isinstance(raw_terms, dict):
        raise ValueError("FORMULA_THESIS.terms must map every AST field to a role")
    terms = {str(field): _text(role, f"terms.{field}")
             for field, role in raw_terms.items()}
    if sorted(terms) != profile["fields"]:
        raise ValueError(
            "FORMULA_THESIS.terms keys must exactly match CANDIDATE_SIGNAL_EXPR fields")
    unknown_roles = sorted(set(terms.values()) - TERM_ROLES)
    if unknown_roles:
        raise ValueError(f"unknown FORMULA_THESIS term roles: {unknown_roles}")
    identification = str(thesis.get("identification") or "").strip()
    if len(identification) < 20:
        raise ValueError("FORMULA_THESIS.identification must be falsifiable and specific")

    degeneracies = _symbolic_degeneracies(candidate)
    if degeneracies:
        raise ValueError(
            "FORMULA_THESIS contains a symbolic degeneracy: "
            + "; ".join(degeneracies)
            + ". REPAIR: remove the cancelled/decorative term and state the "
              "actual economic interaction")
    influence = _term_influence(candidate, profile["fields"])
    presence_only = [field for field, modes in influence.items()
                     if modes == ["PRESENCE_ONLY"]]
    if presence_only:
        raise ValueError(
            "FORMULA_THESIS terms have presence-only influence after sign() "
            f"on non-negative observables: {presence_only}. REPAIR: preserve "
            "their magnitude with a justified transform, use rolling_zscore/"
            "delta for a signed change, or express a true activity state as "
            "an explicit where(gt(...)) gate; do not hide the term in a "
            "denominator")
    directional_value = sorted(
        field for field in _DIRECTIONAL_PRESSURE_FIELDS
        if "VALUE" in influence.get(field, ()) and terms.get(field) == "PRESSURE")
    if not directional_value:
        raise ValueError(
            "FORMULA_THESIS has no signed directional PRESSURE field on the "
            "numeric VALUE path. Activity, spread, depth, and realized-volatility "
            "levels can gate or scale a markout but cannot by themselves identify "
            "up versus down. REPAIR: carry queue imbalance, microprice offset, "
            "trade-flow imbalance, or signed quote OFI into the score; use state "
            "and volatility fields only as explicit gates/scales, then ablate them")
    profile["term_influence"] = influence
    profile["directional_pressure_fields"] = directional_value
    profile["normalization_fields"] = sorted(
        set(profile["fields"]) & _CLOCK_NORMALIZED_FIELDS)
    profile["score_calibration"] = (
        CALIBRATION_CONTRACT if structure_only else "NONE_FIXED_EQUATION")

    operators = set(profile["operators"])
    clocks = profile["clocks_seconds"]
    fields = set(profile["fields"])
    if form == "STATE_CONDITIONAL" and "where" not in operators:
        raise ValueError("STATE_CONDITIONAL thesis requires a where AST node")
    if form == "CROSS_SCALE" and len(clocks) < 2:
        raise ValueError("CROSS_SCALE thesis requires at least two distinct clocks")
    has_l1_l10_pair = bool(fields & _L1_QUOTE_FIELDS) and bool(
        fields & _L10_QUOTE_FIELDS)
    has_explicit_depth_relation = bool(fields & _EXPLICIT_DEPTH_RELATION_FIELDS)
    if form == "DEPTH_DIVERGENCE" and not (
            has_l1_l10_pair or has_explicit_depth_relation):
        raise ValueError(
            "DEPTH_DIVERGENCE thesis requires an explicit depth-relation field "
            "or both L1 and L10 quote fields")
    if form == "L1_L10_CONFIRMATION":
        if not (has_l1_l10_pair or has_explicit_depth_relation):
            raise ValueError(
                "L1_L10_CONFIRMATION thesis requires an explicit depth-relation "
                "field or both L1 and L10 quote fields")
        if not operators & {"abs", "add", "mul", "min", "max", "where"}:
            raise ValueError(
                "L1_L10_CONFIRMATION thesis requires a visible agreement/"
                "distance operator")
    if form == "QUOTE_TAPE_CONFIRMATION":
        if not fields & _QUOTE_PRESSURE_FIELDS or not fields & _TAPE_PRESSURE_FIELDS:
            raise ValueError(
                "QUOTE_TAPE_CONFIRMATION thesis requires signed quote and tape "
                "pressure fields")
        if not operators & {"mul", "min", "max", "where"}:
            raise ValueError(
                "QUOTE_TAPE_CONFIRMATION thesis requires a visible confirmation "
                "gate or interaction")
    if form == "CLOCK_NORMALIZED" and not fields & _CLOCK_NORMALIZED_FIELDS:
        raise ValueError(
            "CLOCK_NORMALIZED thesis requires normalized_quote_ofi_per_event "
            "or quote_ofi_per_trade_volume")
    if form == "INTERACTION" and not (operators & {"mul", "div", "where"}):
        raise ValueError("INTERACTION thesis requires mul, div, or where")
    if form == "REVERSAL" and not (
            operators & {"neg"} or
            str(semantic_plan.get("direction") or "").upper() == "REVERT"):
        raise ValueError("REVERSAL thesis requires neg or Direction=REVERT")

    normalized = {
        "target": target,
        "functional_form": form,
        "expected_sign": sign,
        "coefficient_policy": coefficient_policy,
        "decision_rule": decision_rule,
        "terms": {field: terms[field] for field in sorted(terms)},
        "identification": identification,
    }
    return {
        "formula_discovery_version": CONTRACT_VERSION,
        "formula_contract_complete": True,
        "formula_thesis": normalized,
        "formula_math_profile": profile,
    }


def _selftest() -> None:
    from pathlib import Path
    import sys

    contracts = Path(__file__).resolve().parents[1] / "contracts"
    sys.path.insert(0, str(contracts))
    import intraday_ast_contract as grammar

    expr = {"op": "where", "condition": {"op": "lt", "args": [
        {"op": "field", "field": "spread_bps"},
        {"const": 5, "unit": "BPS"}]}, "then": {
            "op": "mul", "args": [
                {"op": "rolling_mean", "seconds": 30,
                 "arg": {"op": "field", "field": "normalized_quote_ofi"}},
                {"op": "field", "field": "realized_volatility_bps"}]},
        "else": {"const": 0, "unit": "BPS"}}
    result = assess({
        "target": "TAKER_NET_PNL",
        "functional_form": "STATE_CONDITIONAL",
        "expected_sign": "POSITIVE",
        "coefficient_policy": "PREREGISTERED_NO_OOS_FIT",
        "decision_rule": "PREDICTED_MARKOUT_CLEARS_COST",
        "terms": {"spread_bps": "LIQUIDITY",
                  "normalized_quote_ofi": "PRESSURE",
                  "realized_volatility_bps": "VOLATILITY"},
        "identification": "Pressure must predict positive net markout only in tight spreads.",
    }, candidate=expr, semantic_plan={"output": "TAKER_NET_PNL",
                                      "execution": "TAKER"}, grammar=grammar)
    assert result["formula_contract_complete"]


if __name__ == "__main__":
    _selftest()
    print("formula_discovery: typed equation contract passed")
